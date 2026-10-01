/// Dart port of `desktop/auth.py`'s config + password hashing, minus the
/// TOTP/2FA and whitelist pieces (see this app's README - deferred, not a
/// protocol gap, since `unpackAuthRequest` already carries `totp_code` and
/// a later pass could wire it up the same way `verify_password` does
/// here). Persisted via `shared_preferences` rather than a
/// `host_config.json` file, matching how `mobile/controller` persists its
/// own settings (see `models/device.dart`).
library host_config;

import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:shared_preferences/shared_preferences.dart';

class HostConfig {
  static const _key = 'host_config_v1';

  /// Base64 salt + PBKDF2-HMAC-SHA256 hash, exactly like auth.py's
  /// `hash_password` - never the plaintext password itself.
  String? unattendedPasswordSalt;
  String? unattendedPasswordHash;

  /// If true, an incoming connection with no valid unattended password is
  /// shown the in-app accept/reject prompt (`session_confirmation_dialog.dart`)
  /// instead of being auto-rejected - mirrors auth.py's
  /// `prompt_accept`/`confirmation_timeout_seconds`, except the "operator
  /// not present" default is reject-without-a-prompt on mobile rather than
  /// a blocking console `input()` call, since there's no guarantee anyone
  /// is looking at the phone.
  bool promptOnUnrecognizedViewer;
  int confirmationTimeoutSeconds;

  HostConfig({
    this.unattendedPasswordSalt,
    this.unattendedPasswordHash,
    this.promptOnUnrecognizedViewer = true,
    this.confirmationTimeoutSeconds = 20,
  });

  bool get hasUnattendedPassword => unattendedPasswordSalt != null && unattendedPasswordHash != null;

  Map<String, dynamic> toJson() => {
        'unattendedPasswordSalt': unattendedPasswordSalt,
        'unattendedPasswordHash': unattendedPasswordHash,
        'promptOnUnrecognizedViewer': promptOnUnrecognizedViewer,
        'confirmationTimeoutSeconds': confirmationTimeoutSeconds,
      };

  static HostConfig fromJson(Map<String, dynamic> j) => HostConfig(
        unattendedPasswordSalt: j['unattendedPasswordSalt'],
        unattendedPasswordHash: j['unattendedPasswordHash'],
        promptOnUnrecognizedViewer: j['promptOnUnrecognizedViewer'] ?? true,
        confirmationTimeoutSeconds: j['confirmationTimeoutSeconds'] ?? 20,
      );

  static Future<HostConfig> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_key);
    if (raw == null) return HostConfig();
    return HostConfig.fromJson(jsonDecode(raw) as Map<String, dynamic>);
  }

  Future<void> save() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, jsonEncode(toJson()));
  }

  void setUnattendedPassword(String password) {
    final salt = _randomBytes(16);
    final hash = _pbkdf2HmacSha256(utf8.encode(password), salt, 200000, 32);
    unattendedPasswordSalt = base64Encode(salt);
    unattendedPasswordHash = base64Encode(hash);
  }

  void clearUnattendedPassword() {
    unattendedPasswordSalt = null;
    unattendedPasswordHash = null;
  }

  bool verifyPassword(String password) {
    if (!hasUnattendedPassword || password.isEmpty) return false;
    final salt = base64Decode(unattendedPasswordSalt!);
    final expected = base64Decode(unattendedPasswordHash!);
    final digest = _pbkdf2HmacSha256(utf8.encode(password), salt, 200000, 32);
    return _constantTimeEquals(digest, expected);
  }
}

Uint8List _randomBytes(int length) {
  final rnd = Random.secure();
  return Uint8List.fromList(List<int>.generate(length, (_) => rnd.nextInt(256)));
}

bool _constantTimeEquals(List<int> a, List<int> b) {
  if (a.length != b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) {
    diff |= a[i] ^ b[i];
  }
  return diff == 0;
}

/// PBKDF2-HMAC-SHA256, matching `hashlib.pbkdf2_hmac("sha256", ...)` on the
/// desktop side byte-for-byte (same salt, same iteration count, same
/// digest -> same hash for the same password, so a config exported from
/// one side could in principle be read by the other, even though nothing
/// in this codebase does that today). `package:crypto` doesn't ship a
/// PBKDF2 helper directly, so this is the standard construction: derive
/// `keyLength` bytes by concatenating `HMAC(password, salt || blockIndex)`
/// blocks, each re-hashed `iterations` times and XORed together.
Uint8List _pbkdf2HmacSha256(List<int> password, List<int> salt, int iterations, int keyLength) {
  final hmac = Hmac(sha256, password);
  const hashLen = 32; // sha256 output size
  final numBlocks = (keyLength / hashLen).ceil();
  final output = BytesBuilder();

  for (var blockIndex = 1; blockIndex <= numBlocks; blockIndex++) {
    final blockIndexBytes = Uint8List(4)
      ..buffer.asByteData().setUint32(0, blockIndex, Endian.big);
    var u = hmac.convert([...salt, ...blockIndexBytes]).bytes;
    var block = Uint8List.fromList(u);
    for (var i = 1; i < iterations; i++) {
      u = hmac.convert(u).bytes;
      for (var j = 0; j < block.length; j++) {
        block[j] ^= u[j];
      }
    }
    output.add(block);
  }

  return output.toBytes().sublist(0, keyLength);
}
