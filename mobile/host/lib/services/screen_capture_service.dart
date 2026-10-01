/// Phase 6 - "Screen capture permission flow (iOS ReplayKit / Android
/// MediaProjection)" and "Screen mirroring to a remote viewer"
/// (`docs/roadmap.md`). Dart-side interface over a platform channel; the
/// actual capture is native code because neither MediaProjection nor
/// ReplayKit has a Dart/Flutter API - see
/// `android/.../MainActivity.kt` + `ScreenCaptureService.kt` and
/// `ios/Runner/ScreenCaptureBridge.swift` for the two implementations
/// this talks to.
///
/// Frames arrive already JPEG-encoded (the native side does the
/// compression, same as `desktop/host.py`'s `capture_frame` does with
/// Pillow) so `host_session.dart` can hand them straight to
/// `proto.packVideoFrame` without this app needing an image codec of its
/// own in Dart.
library screen_capture_service;

import 'dart:async';

import 'package:flutter/services.dart';

class ScreenDimensions {
  final int width;
  final int height;
  final double devicePixelRatio;
  const ScreenDimensions(this.width, this.height, this.devicePixelRatio);
}

class ScreenCaptureService {
  static const _method = MethodChannel('remote_host/screen_capture');
  static const _frames = EventChannel('remote_host/screen_capture_frames');

  Stream<Uint8List>? _frameStream;

  /// Shows the OS permission prompt (`MediaProjectionManager
  /// .createScreenCaptureIntent()` on Android; `RPScreenRecorder
  /// .startCapture` triggers iOS's own system prompt the first time).
  /// Returns false if the user declined, matching how the auth/whitelist
  /// rejections elsewhere in this app return a plain bool rather than
  /// throwing for an expected "no" answer.
  Future<bool> requestPermission() async {
    try {
      final granted = await _method.invokeMethod<bool>('requestPermission');
      return granted ?? false;
    } on PlatformException {
      return false;
    }
  }

  /// Starts the foreground capture service (Android) / capture session
  /// (iOS). Must be called after [requestPermission] returns true.
  /// [targetFps] and [targetQuality] seed the native encoder's starting
  /// point - `host_session.dart`'s combined adaptive-bitrate/battery cap
  /// updates them per-frame afterwards via [updateEncoderSettings].
  Future<ScreenDimensions> start({required int targetFps, required int targetQuality}) async {
    final result = await _method.invokeMapMethod<String, dynamic>('start', {
      'targetFps': targetFps,
      'targetQuality': targetQuality,
    });
    if (result == null) {
      throw StateError('screen_capture_service: start() returned no dimensions - was requestPermission() granted?');
    }
    return ScreenDimensions(
      result['width'] as int,
      result['height'] as int,
      (result['devicePixelRatio'] as num).toDouble(),
    );
  }

  /// Called once per battery/adaptive-bitrate recomputation (not once per
  /// frame - the native side holds these until the next frame it
  /// captures) so the encoder's JPEG quality and capture interval track
  /// `battery_guard.dart`'s clamp without a full stop/restart of the
  /// capture session.
  Future<void> updateEncoderSettings({required int quality, required int fps}) async {
    await _method.invokeMethod('updateEncoderSettings', {'quality': quality, 'fps': fps});
  }

  /// One JPEG-encoded frame per event, straight off the native encoder.
  Stream<Uint8List> get frames {
    _frameStream ??= _frames.receiveBroadcastStream().map((event) => event as Uint8List);
    return _frameStream!;
  }

  Future<void> stop() async {
    await _method.invokeMethod('stop');
  }
}
