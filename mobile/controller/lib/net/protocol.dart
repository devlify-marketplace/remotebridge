/// Dart port of `desktop/protocol.py`.
///
/// This has to stay byte-for-byte compatible with the Python side, since
/// the host doesn't know or care whether the viewer at the other end of
/// the socket is `desktop/viewer.py` or this app. See protocol.py's
/// module docstring for the framing rules this mirrors:
///
///   [1-byte type][4-byte big-endian length][payload]
///
/// Mouse coordinates are normalized floats in [0.0, 1.0] relative to the
/// host's screen - the host multiplies by its own resolution, so this
/// client never needs to know the remote screen's pixel size.
library protocol;

import 'dart:convert';
import 'dart:typed_data';

class MsgType {
  static const int videoFrame = 0x01;
  static const int mouseMove = 0x02;
  static const int mouseClick = 0x03;
  static const int mouseScroll = 0x04;
  static const int keyEvent = 0x05;

  static const int authRequest = 0x10;
  static const int authResponse = 0x11;

  static const int clipboardText = 0x20;
  static const int clipboardImage = 0x21;

  static const int fileListRequest = 0x30;
  static const int fileListResponse = 0x31;
  static const int fileSendRequest = 0x32;
  static const int fileSendAccept = 0x33;
  static const int fileChunk = 0x34;
  static const int fileComplete = 0x35;
  static const int filePullRequest = 0x36;

  static const int statsReport = 0x40;

  static const int monitorListRequest = 0x50;
  static const int monitorListResponse = 0x51;
  static const int monitorSwitch = 0x52;
}

const Map<String, int> _buttonToByte = {'left': 0, 'right': 1, 'middle': 2};

/// A decoded (type, payload) frame read off the wire.
class WireMessage {
  final int type;
  final Uint8List payload;
  const WireMessage(this.type, this.payload);
}

/// Builds the `[type][length][payload]` header+body a socket write needs.
Uint8List encodeMessage(int msgType, [Uint8List? payload]) {
  final body = payload ?? Uint8List(0);
  final out = ByteData(5 + body.length);
  out.setUint8(0, msgType);
  out.setUint32(1, body.length, Endian.big);
  final bytes = out.buffer.asUint8List();
  bytes.setRange(5, 5 + body.length, body);
  return bytes;
}

// --- Mouse move -----------------------------------------------------------

Uint8List packMouseMove(double xNorm, double yNorm) {
  final b = ByteData(8);
  b.setFloat32(0, xNorm, Endian.big);
  b.setFloat32(4, yNorm, Endian.big);
  return b.buffer.asUint8List();
}

// --- Mouse click ------------------------------------------------------------

Uint8List packMouseClick(double xNorm, double yNorm, String button, bool pressed) {
  final b = ByteData(10);
  b.setFloat32(0, xNorm, Endian.big);
  b.setFloat32(4, yNorm, Endian.big);
  b.setUint8(8, _buttonToByte[button] ?? 0);
  b.setUint8(9, pressed ? 1 : 0);
  return b.buffer.asUint8List();
}

// --- Mouse scroll -------------------------------------------------------

Uint8List packMouseScroll(double dx, double dy) {
  final b = ByteData(8);
  b.setFloat32(0, dx, Endian.big);
  b.setFloat32(4, dy, Endian.big);
  return b.buffer.asUint8List();
}

// --- Keyboard ---------------------------------------------------------------
// key_name matches pynput's naming on the host side: a single printable
// character ("a", "5", "@") or a special-key name ("space", "enter",
// "backspace", "shift", "ctrl_l", "esc", "f1", ...).

Uint8List packKeyEvent(String keyName, bool pressed) {
  final nameBytes = utf8.encode(keyName);
  final out = Uint8List(1 + nameBytes.length);
  out[0] = pressed ? 1 : 0;
  out.setRange(1, out.length, nameBytes);
  return out;
}

// --- Auth handshake -----------------------------------------------------

Uint8List packAuthRequest({required String viewerId, String password = '', String totpCode = ''}) {
  return utf8.encode(jsonEncode({
    'viewer_id': viewerId,
    'password': password,
    'totp_code': totpCode,
  }));
}

Map<String, dynamic> unpackAuthResponse(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

// --- Stats report (feeds the host's existing adaptive-bitrate controller) -

Uint8List packStatsReport(double measuredFps, double measuredKbps) {
  final b = ByteData(8);
  b.setFloat32(0, measuredFps, Endian.big);
  b.setFloat32(4, measuredKbps, Endian.big);
  return b.buffer.asUint8List();
}

// --- Multi-monitor --------------------------------------------------------

Uint8List packMonitorListRequest() => Uint8List(0);

Map<String, dynamic> unpackMonitorListResponse(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

Uint8List packMonitorSwitch(int index) {
  return utf8.encode(jsonEncode({'index': index}));
}

// --- Clipboard (used by the "sync clipboard" quick action) ---------------

Uint8List packClipboardText(String text) => utf8.encode(text);

String unpackClipboardText(Uint8List payload) => utf8.decode(payload);
