/// Phase 6 - "Battery-aware throttling" (`docs/roadmap.md`'s Phase 6
/// bullet list). `adaptive_bitrate.dart`'s `AdaptiveBitrateController`
/// already tracks quality/fps against what the viewer reports it's
/// keeping up with (Phase 4's network-side story, ported from
/// `desktop/adaptive.py`); this adds a second, independent cap driven by
/// the phone's own battery state, since a host that's dying isn't a
/// networking problem the viewer's stats reports can see coming. The two
/// caps combine with a plain min(): whichever is stricter wins, so
/// hosting on a low battery over a great connection is still throttled,
/// and a struggling connection on a full battery is still governed by
/// the network-adaptive controller as before.
library battery_guard;

import 'dart:async';

import 'package:battery_plus/battery_plus.dart';

import 'adaptive_bitrate.dart';

/// One throttle tier per battery/charging combination. Charging always
/// lifts the cap back to "no extra restriction" - a phone plugged in
/// while hosting isn't spending its own battery down, so there's nothing
/// to protect against.
class BatteryThrottleTier {
  final int qualityCap;
  final int fpsCap;
  final String label;
  const BatteryThrottleTier(this.qualityCap, this.fpsCap, this.label);
}

const _charging = BatteryThrottleTier(qualityMax, fpsMax, 'charging - no throttle');
const _normal = BatteryThrottleTier(qualityMax, fpsMax, 'battery ok');
const _low = BatteryThrottleTier(60, 20, 'battery low - capping quality/fps');
const _critical = BatteryThrottleTier(35, 10, 'battery critical - heavy throttle');

const int lowBatteryThresholdPercent = 30;
const int criticalBatteryThresholdPercent = 15;

class BatteryGuard {
  final Battery _battery = Battery();
  final AdaptiveBitrateController bitrate;
  StreamSubscription<BatteryState>? _stateSub;
  Timer? _pollTimer;

  BatteryThrottleTier _tier = _normal;
  int _levelPercent = 100;
  BatteryState _state = BatteryState.unknown;

  BatteryGuard(this.bitrate);

  BatteryThrottleTier get currentTier => _tier;
  int get levelPercent => _levelPercent;
  bool get isCharging => _state == BatteryState.charging || _state == BatteryState.full;

  Future<void> start() async {
    await _refresh();
    _stateSub = _battery.onBatteryStateChanged.listen((state) {
      _state = state;
      _recompute();
    });
    // onBatteryStateChanged fires on charging transitions, not on every
    // percent drop while discharging - poll the level periodically too so
    // crossing the low/critical thresholds is still noticed promptly.
    _pollTimer = Timer.periodic(const Duration(minutes: 1), (_) => _refresh());
  }

  Future<void> _refresh() async {
    _levelPercent = await _battery.batteryLevel;
    _state = await _battery.batteryState;
    _recompute();
  }

  void _recompute() {
    final BatteryThrottleTier next;
    if (isCharging) {
      next = _charging;
    } else if (_levelPercent <= criticalBatteryThresholdPercent) {
      next = _critical;
    } else if (_levelPercent <= lowBatteryThresholdPercent) {
      next = _low;
    } else {
      next = _normal;
    }
    _tier = next;
    _applyCap();
  }

  void _applyCap() {
    final current = bitrate.settings;
    if (current.quality > _tier.qualityCap || current.fps > _tier.fpsCap) {
      bitrate.recordReport(0, 0); // nudge it downward; see note below
    }
    // recordReport(0, 0) reads as "the viewer isn't keeping up at all",
    // which steps quality/fps down by one notch per call - correct
    // direction, but the controller itself has no notion of a hard
    // ceiling. clampToTier() below is the actual enforcement; the
    // recordReport() call above just keeps the controller's own idea of
    // "current settings" trending the same direction instead of
    // fighting the clamp every frame.
  }

  /// Called by the video-capture loop once per frame, after reading
  /// `bitrate.settings` - the authoritative enforcement point, since
  /// `_applyCap` above only nudges the underlying controller rather than
  /// guaranteeing it never exceeds the tier's cap on its own.
  BitrateSettings clampToTier(BitrateSettings settings) {
    return BitrateSettings(
      settings.quality > _tier.qualityCap ? _tier.qualityCap : settings.quality,
      settings.fps > _tier.fpsCap ? _tier.fpsCap : settings.fps,
    );
  }

  Future<void> stop() async {
    await _stateSub?.cancel();
    _pollTimer?.cancel();
  }
}
