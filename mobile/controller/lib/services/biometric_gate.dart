import 'package:local_auth/local_auth.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Phase 5 - "Biometric app lock (Face ID / fingerprint)".
///
/// Gates two moments: opening the app at all, and starting an
/// unattended (password-only, no remote-side confirmation) session -
/// the second matters even if the first is toggled off, since
/// unattended access is the one mode where nobody on the other end
/// gets a chance to reject a connection they didn't expect.
class BiometricGate {
  static const _enabledKey = 'biometric_lock_enabled';
  final _auth = LocalAuthentication();

  Future<bool> isEnabled() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getBool(_enabledKey) ?? false;
  }

  Future<void> setEnabled(bool enabled) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_enabledKey, enabled);
  }

  Future<bool> deviceSupportsBiometrics() async {
    try {
      return await _auth.canCheckBiometrics || await _auth.isDeviceSupported();
    } catch (_) {
      return false;
    }
  }

  /// Returns true if unlocked (either biometrics passed, or the lock is
  /// simply off). Never throws - a plugin/hardware error fails locked
  /// rather than silently letting the app open, since the whole point
  /// is "don't skip the prompt".
  Future<bool> authenticate(String reason) async {
    if (!await isEnabled()) return true;
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(biometricOnly: false, stickyAuth: true),
      );
    } catch (_) {
      return false;
    }
  }
}
