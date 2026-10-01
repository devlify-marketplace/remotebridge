/// Phase 6 - the mobile-host mirror of `desktop/host.py`'s
/// `run_one_session`/`video_loop`/`input_loop`. Brings up the same
/// three channels (video, input, control) the desktop host does, runs
/// the same auth handshake (`auth.py`'s decision logic, ported to
/// `host_config.dart`), and then:
///   - streams JPEG frames from `screen_capture_service.dart` as
///     `MSG_VIDEO_FRAME`s on the video channel (Phase 6's screen
///     mirroring),
///   - drains the input channel without acting on it (see the README -
///     touch/mouse control *of* the phone is deliberately out of scope
///     for this phase, but the channel still needs to exist so an
///     unmodified `desktop/viewer.py` or the Phase 5 controller app can
///     connect without erroring on a missing port/registration),
///   - dispatches the control channel to `HostFileBrowser` (Phase 6's
///     scoped file browser), the adaptive-bitrate controller (Phase 4's
///     `MSG_STATS_REPORT`, unchanged), and a single-entry monitor list
///     representing the phone's own screen (Phase 4's monitor
///     protocol, reused rather than extended, since a phone has exactly
///     one "monitor" to offer).
library host_session;

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:path_provider/path_provider.dart';

import '../models/host_config.dart';
import '../services/adaptive_bitrate.dart';
import '../services/battery_guard.dart';
import '../services/cert_manager.dart';
import '../services/host_file_browser.dart';
import '../services/screen_capture_service.dart';
import '../services/session_log.dart' as session_log;
import 'protocol.dart' as proto;
import 'wire_socket.dart';

/// Either a direct LAN target (three listening ports) or a relay + device
/// ID (three registered names), matching `host.py --video-port ...` /
/// `host.py --relay ... --id ...`.
class HostTarget {
  final bool isRelay;
  final int? videoPort, inputPort, controlPort;
  final String? relayHost;
  final int? relayPort;
  final String? deviceId;
  /// Phase 13: token for a relay that requires authentication; null/empty for an open relay.
  final String? relayToken;

  const HostTarget.direct({required this.videoPort, required this.inputPort, required this.controlPort})
      : isRelay = false, relayHost = null, relayPort = null, deviceId = null, relayToken = null;

  const HostTarget.relay({required this.relayHost, required this.relayPort, required this.deviceId, this.relayToken})
      : isRelay = true, videoPort = null, inputPort = null, controlPort = null;
}

enum SessionOutcome { rejected, ended, error }

/// Callback the UI supplies to actually show the accept/reject dialog
/// (`widgets/session_confirmation_dialog.dart`) when no valid unattended
/// password was presented. Returns false (reject) if it times out -
/// same reject-by-default reasoning as `auth.py`'s `prompt_accept`.
typedef ConfirmationPrompt = Future<bool> Function(String viewerId, String address, int timeoutSeconds);

class HostSession {
  final HostTarget target;
  final HostConfig config;
  final void Function(String message) onLog;
  final ConfirmationPrompt onConfirmationNeeded;
  final void Function(String viewerId, Duration duration)? onSessionEnded;

  final _certManager = CertManager();
  final capture = ScreenCaptureService();
  late final AdaptiveBitrateController bitrate;
  late final BatteryGuard batteryGuard;

  BitrateSettings _lastAppliedSettings = const BitrateSettings(0, 0);
  bool _stopRequested = false;

  HostSession({
    required this.target,
    required this.config,
    required this.onLog,
    required this.onConfirmationNeeded,
    this.onSessionEnded,
  }) {
    bitrate = AdaptiveBitrateController(startQuality: 60, startFps: 15);
    batteryGuard = BatteryGuard(bitrate);
  }

  /// Loops forever accepting one viewer session at a time, matching
  /// `host.py`'s outer `while True: run_one_session(...)`. Call
  /// [requestStop] from the UI to break out after the current session.
  Future<void> run() async {
    await batteryGuard.start();
    final cert = await _certManager.loadOrCreate();
    final context = await _certManager.buildContext(cert);

    while (!_stopRequested) {
      try {
        await _runOneSession(context);
      } catch (e) {
        onLog('session error: $e');
      }
    }
  }

  void requestStop() {
    _stopRequested = true;
  }

  Future<void> _runOneSession(SecurityContext context) async {
    onLog(target.isRelay
        ? "waiting for a viewer via relay as '${target.deviceId}'..."
        : 'waiting for a viewer on port ${target.videoPort}...');

    final video = target.isRelay
        ? await WireSocket.registerRelayTls(target.relayHost!, target.relayPort!, '${target.deviceId}-video', context,
            relayToken: target.relayToken)
        : await WireSocket.listenDirectTls(target.videoPort!, context);

    onLog('viewer connected on video channel, awaiting authentication...');

    final firstMessage = await video.messages.first;
    if (firstMessage.type != proto.MsgType.authRequest) {
      onLog('protocol error: expected an auth request first');
      await video.close();
      return;
    }
    final req = proto.unpackAuthRequest(firstMessage.payload);
    final viewerId = (req['viewer_id'] as String?)?.trim().isNotEmpty == true ? req['viewer_id'] as String : 'unknown';
    final password = req['password'] as String? ?? '';

    final decision = await _decide(viewerId, password);
    await session_log.logEvent('attempt', {
      'viewer_id': viewerId,
      'decision': decision.decision,
      'reason': decision.reason,
    });

    video.send(proto.MsgType.authResponse,
        proto.packAuthResponse(approved: decision.approved, reason: decision.reason));

    if (!decision.approved) {
      onLog("connection rejected (${decision.decision}): ${decision.reason}");
      await video.close();
      return;
    }
    onLog("connection approved (${decision.decision}) for viewer '$viewerId'");

    final input = target.isRelay
        ? await WireSocket.registerRelayTls(target.relayHost!, target.relayPort!, '${target.deviceId}-input', context,
            relayToken: target.relayToken)
        : await WireSocket.listenDirectTls(target.inputPort!, context);
    final control = target.isRelay
        ? await WireSocket.registerRelayTls(
            target.relayHost!, target.relayPort!, '${target.deviceId}-control', context,
            relayToken: target.relayToken)
        : await WireSocket.listenDirectTls(target.controlPort!, context);

    final timer = session_log.SessionTimer();
    await session_log.logEvent('start', {'viewer_id': viewerId});

    final dims = await capture.start(targetFps: 15, targetQuality: 60);
    final rootDir = await getApplicationDocumentsDirectory();
    final fileBrowser = HostFileBrowser(control: control, rootDir: rootDir, onLog: onLog);

    final inputDrainSub = input.messages.listen((_) {
      // Deliberately not acted on - see this file's docstring and the
      // README's "Deliberately deferred" section. Still consumed so the
      // socket's read buffer doesn't grow unbounded for the session.
    });

    final controlSub = control.messages.listen((m) {
      if (m.type == proto.MsgType.statsReport) {
        final s = proto.unpackStatsReport(m.payload);
        bitrate.recordReport(s.measuredFps, s.measuredKbps);
      } else if (m.type == proto.MsgType.monitorListRequest) {
        control.send(
          proto.MsgType.monitorListResponse,
          proto.packMonitorListResponse(
            [
              {'index': 1, 'width': dims.width, 'height': dims.height, 'left': 0, 'top': 0}
            ],
            1,
          ),
        );
      } else if (m.type == proto.MsgType.monitorSwitch) {
        // Only one "monitor" exists on a phone - nothing to switch to.
        proto.unpackMonitorSwitch(m.payload);
      } else if (m.type == proto.MsgType.clipboardText) {
        // Applying an incoming clipboard text to the OS clipboard needs
        // `package:flutter/services.dart`'s Clipboard.setData, which
        // pulls a Flutter UI dependency into what's otherwise a plain
        // Dart net/ layer - left to host_setup_screen.dart to wire up by
        // listening on a callback here in a later pass; not done yet
        // (see README).
      } else {
        fileBrowser.dispatch(m);
      }
    });

    bool videoDropped = false;
    final frameSub = capture.frames.listen((jpeg) {
      final settings = batteryGuard.clampToTier(bitrate.settings);
      if (settings.quality != _lastAppliedSettings.quality || settings.fps != _lastAppliedSettings.fps) {
        capture.updateEncoderSettings(quality: settings.quality, fps: settings.fps);
        _lastAppliedSettings = settings;
      }
      try {
        video.send(proto.MsgType.videoFrame, proto.packVideoFrame(jpeg));
      } catch (_) {
        videoDropped = true;
      }
    });

    // Runs until the video channel closes (viewer disconnected) or an
    // explicit stop is requested - mirrors host.py's video_loop being the
    // thing run_one_session blocks on.
    final emptyPayload = Uint8List(0);
    await video.messages
        .firstWhere((_) => false, orElse: () => proto.WireMessage(-1, emptyPayload))
        .catchError((_) => proto.WireMessage(-1, emptyPayload));
    await for (final _ in Stream.periodic(const Duration(milliseconds: 250))) {
      if (videoDropped || _stopRequested) break;
    }

    await frameSub.cancel();
    await controlSub.cancel();
    await inputDrainSub.cancel();
    await capture.stop();
    await video.close();
    await input.close();
    await control.close();

    final duration = Duration(milliseconds: (timer.elapsedSeconds() * 1000).round());
    await session_log.logEvent('end', {'viewer_id': viewerId, 'duration_seconds': timer.elapsedSeconds()});
    onSessionEnded?.call(viewerId, duration);
  }

  Future<({bool approved, String decision, String reason})> _decide(String viewerId, String password) async {
    if (config.hasUnattendedPassword && password.isNotEmpty) {
      if (config.verifyPassword(password)) {
        return (approved: true, decision: 'auto_unattended', reason: 'unattended password verified');
      }
      return (approved: false, decision: 'rejected_password', reason: 'incorrect unattended-access password');
    }

    if (!config.promptOnUnrecognizedViewer) {
      return (
        approved: false,
        decision: 'rejected_no_prompt',
        reason: 'no valid unattended password and prompting is disabled'
      );
    }

    final accepted = await onConfirmationNeeded(viewerId, target.isRelay ? target.deviceId ?? '' : 'LAN',
            config.confirmationTimeoutSeconds)
        .timeout(Duration(seconds: config.confirmationTimeoutSeconds), onTimeout: () => false);

    if (accepted) {
      return (approved: true, decision: 'manual_accept', reason: 'accepted on the host phone');
    }
    return (approved: false, decision: 'rejected_manual', reason: 'declined or timed out on the host phone');
  }
}
