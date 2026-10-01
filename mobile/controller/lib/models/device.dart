import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// A saved connection target - either a direct LAN host or a relay
/// device ID. Powers the Phase 5 "session picker optimized for small
/// screens (favorites list, recent connections, search)" item, and
/// doubles as the "offline device list caching" item: this list is
/// local and always shows even with no live connection.
class SavedDevice {
  final String label;
  final bool isRelay;
  // Direct mode:
  final String? host;
  final int? videoPort, inputPort, controlPort;
  // Relay mode:
  final String? relayHost;
  final int? relayPort;
  final String? deviceId;

  bool favorite;
  DateTime lastConnected;

  SavedDevice.direct({
    required this.label,
    required this.host,
    required this.videoPort,
    required this.inputPort,
    required this.controlPort,
    this.favorite = false,
    DateTime? lastConnected,
  })  : isRelay = false,
        relayHost = null,
        relayPort = null,
        deviceId = null,
        lastConnected = lastConnected ?? DateTime.now();

  SavedDevice.relay({
    required this.label,
    required this.relayHost,
    required this.relayPort,
    required this.deviceId,
    this.favorite = false,
    DateTime? lastConnected,
  })  : isRelay = true,
        host = null,
        videoPort = null,
        inputPort = null,
        controlPort = null,
        lastConnected = lastConnected ?? DateTime.now();

  Map<String, dynamic> toJson() => {
        'label': label,
        'isRelay': isRelay,
        'host': host,
        'videoPort': videoPort,
        'inputPort': inputPort,
        'controlPort': controlPort,
        'relayHost': relayHost,
        'relayPort': relayPort,
        'deviceId': deviceId,
        'favorite': favorite,
        'lastConnected': lastConnected.toIso8601String(),
      };

  static SavedDevice fromJson(Map<String, dynamic> j) {
    if (j['isRelay'] == true) {
      return SavedDevice.relay(
        label: j['label'],
        relayHost: j['relayHost'],
        relayPort: j['relayPort'],
        deviceId: j['deviceId'],
        favorite: j['favorite'] ?? false,
        lastConnected: DateTime.parse(j['lastConnected']),
      );
    }
    return SavedDevice.direct(
      label: j['label'],
      host: j['host'],
      videoPort: j['videoPort'],
      inputPort: j['inputPort'],
      controlPort: j['controlPort'],
      favorite: j['favorite'] ?? false,
      lastConnected: DateTime.parse(j['lastConnected']),
    );
  }
}

class DeviceStore {
  static const _key = 'saved_devices_v1';

  Future<List<SavedDevice>> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_key);
    if (raw == null) return [];
    final list = jsonDecode(raw) as List<dynamic>;
    return list.map((e) => SavedDevice.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> save(List<SavedDevice> devices) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, jsonEncode(devices.map((d) => d.toJson()).toList()));
  }

  Future<void> upsert(SavedDevice device) async {
    final devices = await load();
    devices.removeWhere((d) => d.label == device.label);
    devices.add(device);
    await save(devices);
  }

  Future<void> touchLastConnected(String label) async {
    final devices = await load();
    for (final d in devices) {
      if (d.label == label) d.lastConnected = DateTime.now();
    }
    await save(devices);
  }

  Future<void> setFavorite(String label, bool favorite) async {
    final devices = await load();
    for (final d in devices) {
      if (d.label == label) d.favorite = favorite;
    }
    await save(devices);
  }

  Future<void> remove(String label) async {
    final devices = await load();
    devices.removeWhere((d) => d.label == label);
    await save(devices);
  }
}
