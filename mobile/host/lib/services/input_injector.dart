import 'package:flutter/services.dart';

/// RemoteBridge Android Input Injector Service
///
/// Dispatches remote viewer touch, swipe, tap, and key events to the Android OS
/// via native AccessibilityService platform channels.
class InputInjectorService {
  static const MethodChannel _channel = MethodChannel('com.remotebridge.host/input_injector');

  /// Inject a tap gesture at normalized coordinates (x: 0.0 - 1.0, y: 0.0 - 1.0)
  static Future<bool> injectTap(double x, double y) async {
    try {
      final bool result = await _channel.invokeMethod('injectTap', {
        'x': x,
        'y': y,
      });
      return result;
    } on PlatformException catch (e) {
      print('InputInjector: Failed to inject tap - ${e.message}');
      return false;
    }
  }

  /// Inject a swipe/drag gesture from (startX, startY) to (endX, endY) over durationMs
  static Future<bool> injectSwipe(double startX, double startY, double endX, double endY, int durationMs) async {
    try {
      final bool result = await _channel.invokeMethod('injectSwipe', {
        'startX': startX,
        'startY': startY,
        'endX': endX,
        'endY': endY,
        'durationMs': durationMs,
      });
      return result;
    } on PlatformException catch (e) {
      print('InputInjector: Failed to inject swipe - ${e.message}');
      return false;
    }
  }

  /// Inject system action (back = 1, home = 2, recents = 3)
  static Future<bool> injectSystemAction(int actionCode) async {
    try {
      final bool result = await _channel.invokeMethod('injectSystemAction', {
        'actionCode': actionCode,
      });
      return result;
    } on PlatformException catch (e) {
      print('InputInjector: Failed to inject system action - ${e.message}');
      return false;
    }
  }
}
