/// Brings up the three sockets a session needs (video, input, control -
/// matching `desktop/host.py`'s three listening ports) and does the auth
/// handshake on the video channel, same as `desktop/viewer.py`.
library session_client;

import 'dart:async';
import 'dart:typed_data';

import 'protocol.dart';
import 'wire_socket.dart';

/// Either a direct LAN target (host:port triplet) or a relay + device ID,
/// matching the two modes `viewer.py --host ...` / `viewer.py --relay
/// ... --id ...` support.
class ConnectTarget {
  final String? directHost;
  final int? videoPort, inputPort, controlPort;
  final String? relayHost;
  final int? relayPort;
  final String? deviceId;

  const ConnectTarget.direct(this.directHost, {required this.videoPort, required this.inputPort, required this.controlPort})
      : relayHost = null, relayPort = null, deviceId = null;

  const ConnectTarget.relay(this.relayHost, this.relayPort, this.deviceId)
      : directHost = null, videoPort = null, inputPort = null, controlPort = null;

  bool get isRelay => relayHost != null;
}

class AuthResult {
  final bool approved;
  final String reason;
  const AuthResult(this.approved, this.reason);
}

enum InputMode { directTouch, trackpad }

class SessionClient {
  final ConnectTarget target;
  final String viewerId;
  final String password;
  final String totpCode;

  WireSocket? _video;
  WireSocket? _input;
  WireSocket? _control;

  final _frames = StreamController<Uint8List>.broadcast();
  final _monitorUpdates = StreamController<Map<String, dynamic>>.broadcast();

  /// Scales the kbps we *report* to the host's adaptive-bitrate
  /// controller (see desktop/adaptive.py). We don't need a new wire
  /// message for "low-data mode" - phase 4 already built a controller
  /// that backs off quality/fps when the viewer reports it's not
  /// keeping up, so under-reporting achieves the same thing with the
  /// protocol as-is.
  bool lowDataMode = false;

  int _framesSinceReport = 0;
  int _bytesSinceReport = 0;
  Timer? _statsTimer;

  SessionClient({required this.target, required this.viewerId, this.password = '', this.totpCode = ''});

  Stream<Uint8List> get frames => _frames.stream;
  Stream<Map<String, dynamic>> get monitorUpdates => _monitorUpdates.stream;

  Future<AuthResult> connect() async {
    if (target.isRelay) {
      _video = await _relayConnect('${target.deviceId}-video');
      _input = await _relayConnect('${target.deviceId}-input');
      _control = await _relayConnect('${target.deviceId}-control');
    } else {
      _video = await WireSocket.connectTls(target.directHost!, target.videoPort!);
      _input = await WireSocket.connectTls(target.directHost!, target.inputPort!);
      _control = await WireSocket.connectTls(target.directHost!, target.controlPort!);
    }

    _video!.send(MsgType.authRequest, packAuthRequest(viewerId: viewerId, password: password, totpCode: totpCode));
    final first = await _video!.messages.first;
    if (first.type != MsgType.authResponse) {
      return const AuthResult(false, 'unexpected message from host during auth');
    }
    final resp = unpackAuthResponse(first.payload);
    final approved = resp['approved'] == true;
    if (!approved) {
      return AuthResult(false, (resp['reason'] ?? 'rejected').toString());
    }

    _video!.messages.listen((m) {
      if (m.type == MsgType.videoFrame) {
        _framesSinceReport++;
        _bytesSinceReport += m.payload.length;
        _frames.add(m.payload);
      }
    });

    _control!.messages.listen((m) {
      if (m.type == MsgType.monitorListResponse) {
        _monitorUpdates.add(unpackMonitorListResponse(m.payload));
      }
    });

    _statsTimer = Timer.periodic(const Duration(seconds: 1), (_) => _reportStats());
    requestMonitorList();
    return const AuthResult(true, '');
  }

  Future<WireSocket> _relayConnect(String name) {
    return WireSocket.connectRelayTls(target.relayHost!, target.relayPort!, name);
  }

  void _reportStats() {
    if (_control == null) return;
    var kbps = _bytesSinceReport * 8 / 1000.0;
    var fps = _framesSinceReport.toDouble();
    if (lowDataMode) {
      // Under-report so the host's existing adaptive controller reads
      // this the same way it'd read a genuinely poor cellular link and
      // steps quality/fps down on its own - no new message type needed.
      kbps *= 0.35;
      fps *= 0.6;
    }
    _control!.send(MsgType.statsReport, packStatsReport(fps, kbps));
    _framesSinceReport = 0;
    _bytesSinceReport = 0;
  }

  // --- Input senders (called from gesture handlers in remote_control_screen) -

  void sendMouseMove(double xNorm, double yNorm) {
    _input?.send(MsgType.mouseMove, packMouseMove(xNorm, yNorm));
  }

  void sendMouseClick(double xNorm, double yNorm, String button, bool pressed) {
    _input?.send(MsgType.mouseClick, packMouseClick(xNorm, yNorm, button, pressed));
  }

  void sendMouseScroll(double dx, double dy) {
    _input?.send(MsgType.mouseScroll, packMouseScroll(dx, dy));
  }

  void sendKey(String keyName, bool pressed) {
    _input?.send(MsgType.keyEvent, packKeyEvent(keyName, pressed));
  }

  /// Convenience for a full key tap (e.g. the on-screen keyboard's
  /// Esc/F-key row, or a gesture shortcut like Alt+Tab).
  void tapKeys(List<String> keyNames) {
    for (final k in keyNames) {
      sendKey(k, true);
    }
    for (final k in keyNames.reversed) {
      sendKey(k, false);
    }
  }

  void requestMonitorList() {
    _control?.send(MsgType.monitorListRequest, packMonitorListRequest());
  }

  void switchMonitor(int index) {
    _control?.send(MsgType.monitorSwitch, packMonitorSwitch(index));
  }

  Future<void> disconnect() async {
    _statsTimer?.cancel();
    await _video?.close();
    await _input?.close();
    await _control?.close();
  }
}
