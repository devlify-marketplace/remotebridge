/// Wraps a `dart:io` Socket/SecureSocket with the same
/// `[type][length][payload]` framing `protocol.py`'s `send_message` /
/// `recv_message` use, since the raw socket API only hands you whatever
/// chunk the OS felt like delivering - it doesn't know about message
/// boundaries.
library wire_socket;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'protocol.dart';

class WireSocket {
  final Socket _socket;
  final _buffer = BytesBuilder(copy: false);
  final _pending = StreamController<WireMessage>.broadcast();
  StreamSubscription<Uint8List>? _sub;

  /// [initialBytes] carries over anything read past the relay's
  /// handshake line (see [connectRelayTls]) that already belongs to the
  /// framed protocol, so nothing gets dropped on the floor.
  WireSocket._(this._socket, {Uint8List? initialBytes}) {
    if (initialBytes != null && initialBytes.isNotEmpty) {
      _buffer.add(initialBytes);
    }
    _sub = _socket.listen(
      (chunk) {
        _buffer.add(chunk);
        _drain();
      },
      onError: (Object e, StackTrace st) => _pending.addError(e, st),
      onDone: () => _pending.close(),
      cancelOnError: false,
    );
    if (initialBytes != null && initialBytes.isNotEmpty) {
      _drain();
    }
  }

  /// Connects a plain TCP socket (LAN/relay use this; the desktop side
  /// wraps it in TLS via `ssl.wrap_socket`, matched below by
  /// [connectTls]).
  static Future<WireSocket> connect(String host, int port, {Duration timeout = const Duration(seconds: 8)}) async {
    final s = await Socket.connect(host, port, timeout: timeout);
    s.setOption(SocketOption.tcpNoDelay, true);
    return WireSocket._(s);
  }

  /// TLS connection. `onBadCertificate` currently trusts whatever the
  /// host presents - see the note in `desktop/README.md`: the desktop
  /// viewer doesn't pin the host's certificate fingerprint yet either,
  /// so this matches the current (pre-hardening) trust model rather
  /// than silently being stricter and failing to connect.
  static Future<WireSocket> connectTls(String host, int port, {Duration timeout = const Duration(seconds: 8)}) async {
    final s = await SecureSocket.connect(
      host,
      port,
      timeout: timeout,
      onBadCertificate: (cert) => true,
    );
    s.setOption(SocketOption.tcpNoDelay, true);
    return WireSocket._(s);
  }

  /// Connects through `server/relay.py` by device ID instead of by
  /// IP:port. The relay speaks one line of plaintext before it starts
  /// pumping raw bytes between the two ends (`CONNECT <name>\n` ->
  /// `OK\n` or `ERROR <reason>\n`) - see relay.py's module docstring -
  /// so this reads that single line by hand before treating the rest of
  /// the stream as the framed video/input/control protocol.
  static Future<WireSocket> connectRelayTls(String relayHost, int relayPort, String name,
      {Duration timeout = const Duration(seconds: 8)}) async {
    final s = await SecureSocket.connect(
      relayHost,
      relayPort,
      timeout: timeout,
      onBadCertificate: (cert) => true,
    );
    s.setOption(SocketOption.tcpNoDelay, true);
    s.add(utf8.encode('CONNECT $name\n'));

    final completer = Completer<Uint8List>();
    final lineBuf = BytesBuilder(copy: false);
    late StreamSubscription<Uint8List> sub;
    sub = s.listen(
      (chunk) {
        lineBuf.add(chunk);
        final bytes = lineBuf.toBytes();
        final newlineAt = bytes.indexOf(10); // '\n'
        if (newlineAt == -1) return;
        sub.cancel();
        final line = utf8.decode(bytes.sublist(0, newlineAt)).trim();
        final rest = Uint8List.sublistView(bytes, newlineAt + 1);
        if (line == 'OK') {
          completer.complete(rest);
        } else {
          completer.completeError(StateError('relay CONNECT $name failed: $line'));
        }
      },
      onError: (Object e, StackTrace st) {
        if (!completer.isCompleted) completer.completeError(e, st);
      },
      onDone: () {
        if (!completer.isCompleted) {
          completer.completeError(StateError('relay closed the connection before responding'));
        }
      },
      cancelOnError: false,
    );

    final leftover = await completer.future;
    return WireSocket._(s, initialBytes: leftover);
  }

  Stream<WireMessage> get messages => _pending.stream;

  void send(int msgType, [Uint8List? payload]) {
    _socket.add(encodeMessage(msgType, payload));
  }

  Future<void> flush() => _socket.flush();

  void _drain() {
    // A message needs at least the 5-byte header before we know its
    // length, and then that many more bytes for the payload. Loop
    // because one TCP read can easily contain several small messages
    // (mouse-move floods, key events) back to back.
    while (true) {
      final bytes = _buffer.toBytes();
      if (bytes.length < 5) return;
      final view = ByteData.sublistView(bytes);
      final msgType = view.getUint8(0);
      final length = view.getUint32(1, Endian.big);
      if (bytes.length < 5 + length) return;
      final payload = Uint8List.sublistView(bytes, 5, 5 + length);
      _pending.add(WireMessage(msgType, payload));
      _buffer.clear();
      if (bytes.length > 5 + length) {
        _buffer.add(bytes.sublist(5 + length));
      }
    }
  }

  Future<void> close() async {
    await _sub?.cancel();
    await _pending.close();
    await _socket.close();
  }
}
