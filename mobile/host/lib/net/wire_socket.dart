/// Wraps a `dart:io` Socket/SecureSocket with the same
/// `[type][length][payload]` framing `protocol.py`'s `send_message` /
/// `recv_message` use - identical to
/// `mobile/controller/lib/net/wire_socket.dart`'s `WireSocket`, since the
/// framing doesn't care which side of the connection you're on. What's
/// different here is *how the socket gets established*: a host is the TLS
/// server, not the client, so this file adds the two connection modes
/// `desktop/host.py` supports (direct listen, relay register-then-upgrade)
/// instead of the controller's two "dial out" modes.
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

  /// Direct mode, matching `host.py`'s `get_raw_channel_direct` +
  /// `tls_ctx.wrap_socket(raw, server_side=True)`: bind, wait for exactly
  /// one incoming connection on [port], complete the TLS handshake as the
  /// server, and return it wrapped. `SecureServerSocket.bind` does the
  /// listen+accept+handshake in one call, unlike the Python stdlib's
  /// separate `bind`/`listen`/`accept`/`wrap_socket` steps.
  static Future<WireSocket> listenDirectTls(int port, SecurityContext context) async {
    final serverSocket = await SecureServerSocket.bind(InternetAddress.anyIPv4, port, context);
    final socket = await serverSocket.first;
    await serverSocket.close();
    socket.setOption(SocketOption.tcpNoDelay, true);
    return WireSocket._(socket);
  }

  /// Relay mode, matching `host.py`'s `get_raw_channel_relay` +
  /// `tls_ctx.wrap_socket(raw, server_side=True)`: connect out to
  /// `server/relay.py` as a plain TCP client, register under [name], then
  /// upgrade that same socket to TLS *as the server side* once a viewer
  /// gets paired to it by the relay. `SecureSocket.secureServer` is the
  /// piece that makes this possible on an already-connected socket instead
  /// of requiring a fresh listen.
  static Future<WireSocket> registerRelayTls(
    String relayHost,
    int relayPort,
    String name,
    SecurityContext context, {
    Duration timeout = const Duration(seconds: 8),
    // Phase 13: per-device token for a relay started with a secret (server/relay_token.py,
    // or GET /api/v1/relay-token on the admin console). Null/empty = open relay.
    String? relayToken,
  }) async {
    final raw = await Socket.connect(relayHost, relayPort, timeout: timeout);
    final suffix = (relayToken == null || relayToken.isEmpty) ? '' : ' $relayToken';
    raw.add(utf8.encode('REGISTER $name$suffix\n'));
    await raw.flush();
    // The relay doesn't reply to REGISTER - it just holds the connection
    // open until a matching CONNECT arrives (see relay.py's module
    // docstring) - so there's nothing to read here before the TLS upgrade,
    // unlike the viewer's CONNECT path which reads an OK/ERROR line first.
    final secure = await SecureSocket.secureServer(raw, context);
    secure.setOption(SocketOption.tcpNoDelay, true);
    return WireSocket._(secure);
  }

  Stream<WireMessage> get messages => _pending.stream;

  void send(int msgType, [Uint8List? payload]) {
    _socket.add(encodeMessage(msgType, payload));
  }

  Future<void> flush() => _socket.flush();

  void _drain() {
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
