import 'package:flutter/services.dart';

/// Phase 5 - "Orientation lock / auto-rotate to match remote screen".
///
/// The host's monitor list (see protocol MSG_MONITOR_LIST_RESPONSE)
/// includes width/height, so this can pick a sensible default
/// orientation for whichever monitor is active, then let the person
/// override it with a lock.
class OrientationService {
  bool _locked = false;
  DeviceOrientation? _lockedTo;

  bool get isLocked => _locked;

  /// Call once a monitor's dimensions are known (or change, after a
  /// monitor switch) to auto-rotate the app to match, unless the person
  /// has locked orientation manually.
  Future<void> matchRemoteAspect(int widthPx, int heightPx) async {
    if (_locked) return;
    final remoteIsLandscape = widthPx >= heightPx;
    await SystemChrome.setPreferredOrientations(remoteIsLandscape
        ? [DeviceOrientation.landscapeLeft, DeviceOrientation.landscapeRight]
        : [DeviceOrientation.portraitUp]);
  }

  Future<void> lockTo(DeviceOrientation orientation) async {
    _locked = true;
    _lockedTo = orientation;
    await SystemChrome.setPreferredOrientations([orientation]);
  }

  Future<void> unlock() async {
    _locked = false;
    _lockedTo = null;
    await SystemChrome.setPreferredOrientations(DeviceOrientation.values);
  }

  DeviceOrientation? get lockedOrientation => _lockedTo;
}
