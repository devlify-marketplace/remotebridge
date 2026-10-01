/// Dart port of `desktop/adaptive.py`'s `AdaptiveBitrateController` (host
/// side only - the phone host has no need for the viewer-side
/// `StatsReporter`, since it isn't the one measuring what it's receiving;
/// see `session_client.dart`'s existing stats reporter in the controller
/// app for that half). Same step sizes and thresholds, same "quality
/// drops before fps on the way down, fps rises before quality on the way
/// up" reasoning from the Python docstring.
library adaptive_bitrate;

const int qualityMin = 20, qualityMax = 90;
const int fpsMin = 5, fpsMax = 30;
const int qualityStep = 10, fpsStep = 2;
const int goodStreakToRaise = 5;
const double keepingUpRatio = 0.85;

class BitrateSettings {
  final int quality;
  final int fps;
  const BitrateSettings(this.quality, this.fps);
}

class AdaptiveBitrateController {
  bool enabled;
  int _quality;
  int _fps;
  int _goodStreak = 0;

  AdaptiveBitrateController({required int startQuality, required int startFps, this.enabled = true})
      : _quality = startQuality.clamp(qualityMin, qualityMax),
        _fps = startFps > 0 ? startFps.clamp(fpsMin, fpsMax) : fpsMax;

  BitrateSettings get settings => BitrateSettings(_quality, _fps);

  void recordReport(double measuredFps, double measuredKbps) {
    if (!enabled) return;
    final targetFps = _fps;
    final keepingUp = targetFps <= 0 || measuredFps >= targetFps * keepingUpRatio;

    if (keepingUp) {
      _goodStreak++;
      if (_goodStreak >= goodStreakToRaise) {
        _goodStreak = 0;
        if (_fps < fpsMax) {
          _fps = (_fps + fpsStep).clamp(fpsMin, fpsMax);
        } else if (_quality < qualityMax) {
          _quality = (_quality + qualityStep).clamp(qualityMin, qualityMax);
        }
      }
    } else {
      _goodStreak = 0;
      if (_quality > qualityMin) {
        _quality = (_quality - qualityStep).clamp(qualityMin, qualityMax);
      } else if (_fps > fpsMin) {
        _fps = (_fps - fpsStep).clamp(fpsMin, fpsMax);
      }
    }
  }
}
