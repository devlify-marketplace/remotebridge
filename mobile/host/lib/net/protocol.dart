/// Dart port of `desktop/protocol.py`, host-side.
///
/// `mobile/controller/lib/net/protocol.dart` ports the *viewer* half of
/// the wire protocol (it sends input/auth-request/stats, receives
/// video/auth-response/monitor-list). This is the mirror image for a
/// host role: sends video frames, auth responses, and file/monitor
/// responses; receives input events, auth requests, file requests, and
/// stats reports. Framing is identical - see that file's docstring, or
/// protocol.py's, for the `[1-byte type][4-byte big-endian length]
/// [payload]` rule both ends share.
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

const Map<int, String> _byteToButton = {0: 'left', 1: 'right', 2: 'middle'};

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

// --- Video frame (host sends; payload is raw JPEG bytes, same as host.py's
// capture_frame) ------------------------------------------------------------

Uint8List packVideoFrame(Uint8List jpegBytes) => jpegBytes;

// --- Mouse / keyboard (host receives, from the input channel) -------------
// Phase 6 accepts these so an unmodified desktop viewer.py or the Phase 5
// controller app can open the input channel without erroring, but a phone
// host doesn't inject them anywhere yet - see host_session.dart and the
// README's "Deliberately deferred" section for why.

({double x, double y}) unpackMouseMove(Uint8List payload) {
  final v = ByteData.sublistView(payload);
  return (x: v.getFloat32(0, Endian.big), y: v.getFloat32(4, Endian.big));
}

({double x, double y, String button, bool pressed}) unpackMouseClick(Uint8List payload) {
  final v = ByteData.sublistView(payload);
  return (
    x: v.getFloat32(0, Endian.big),
    y: v.getFloat32(4, Endian.big),
    button: _byteToButton[v.getUint8(8)] ?? 'left',
    pressed: v.getUint8(9) != 0,
  );
}

({double dx, double dy}) unpackMouseScroll(Uint8List payload) {
  final v = ByteData.sublistView(payload);
  return (dx: v.getFloat32(0, Endian.big), dy: v.getFloat32(4, Endian.big));
}

({String keyName, bool pressed}) unpackKeyEvent(Uint8List payload) {
  return (pressed: payload[0] != 0, keyName: utf8.decode(payload.sublist(1)));
}

// --- Auth handshake ---------------------------------------------------------

Map<String, dynamic> unpackAuthRequest(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

Uint8List packAuthResponse({required bool approved, String reason = ''}) {
  return utf8.encode(jsonEncode({'approved': approved, 'reason': reason}));
}

// --- File transfer & remote browsing (control channel; host role) ---------

Map<String, dynamic> unpackFileListRequest(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

Uint8List packFileListResponse(String directory, List<Map<String, dynamic>> entries) {
  return utf8.encode(jsonEncode({'dir': directory, 'entries': entries}));
}

Map<String, dynamic> unpackFileSendRequest(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

/// The host also needs to *send* a send-request when a viewer's pull
/// request (`MSG_FILE_PULL_REQUEST`) is answered by pushing a file back -
/// see `host_file_browser.dart`'s `_sendFile`.
Uint8List packFileSendRequest(int transferId, String filename, int size) {
  return utf8.encode(jsonEncode({'transfer_id': transferId, 'filename': filename, 'size': size}));
}

Uint8List packFileSendAccept({
  required int transferId,
  required bool accepted,
  required int resumeOffset,
  String reason = '',
}) {
  return utf8.encode(jsonEncode({
    'transfer_id': transferId,
    'accepted': accepted,
    'resume_offset': resumeOffset,
    'reason': reason,
  }));
}

Map<String, dynamic> unpackFileSendAccept(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

const int _chunkHeaderSize = 4 + 8; // transferId (uint32) + offset (uint64)

Uint8List packFileChunk(int transferId, int offset, Uint8List data) {
  final out = Uint8List(_chunkHeaderSize + data.length);
  final v = ByteData.sublistView(out);
  v.setUint32(0, transferId, Endian.big);
  v.setUint64(4, offset, Endian.big);
  out.setRange(_chunkHeaderSize, out.length, data);
  return out;
}

({int transferId, int offset, Uint8List data}) unpackFileChunk(Uint8List payload) {
  final v = ByteData.sublistView(payload, 0, _chunkHeaderSize);
  return (
    transferId: v.getUint32(0, Endian.big),
    offset: v.getUint64(4, Endian.big),
    data: Uint8List.sublistView(payload, _chunkHeaderSize),
  );
}

Uint8List packFileComplete(int transferId, bool ok, [String message = '']) {
  return utf8.encode(jsonEncode({'transfer_id': transferId, 'ok': ok, 'message': message}));
}

Map<String, dynamic> unpackFileComplete(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

Map<String, dynamic> unpackFilePullRequest(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

Uint8List packFilePullRequest(String path) => utf8.encode(jsonEncode({'path': path}));

// --- Adaptive bitrate feedback (host receives, from the control channel) --

({double measuredFps, double measuredKbps}) unpackStatsReport(Uint8List payload) {
  final v = ByteData.sublistView(payload);
  return (measuredFps: v.getFloat32(0, Endian.big), measuredKbps: v.getFloat32(4, Endian.big));
}

// --- Multi-monitor (host reports exactly one "monitor": its own screen) ---

Uint8List packMonitorListResponse(List<Map<String, dynamic>> monitors, int activeIndex) {
  return utf8.encode(jsonEncode({'monitors': monitors, 'active': activeIndex}));
}

Map<String, dynamic> unpackMonitorSwitch(Uint8List payload) {
  return jsonDecode(utf8.decode(payload)) as Map<String, dynamic>;
}

// --- Clipboard (host role: accept incoming text, matching clipboard_sync.py;
// writing it to the OS clipboard is Flutter's Clipboard.setData, done in
// host_session.dart rather than here) ---------------------------------------

String unpackClipboardText(Uint8List payload) => utf8.decode(payload);

Uint8List packClipboardText(String text) => utf8.encode(text);
