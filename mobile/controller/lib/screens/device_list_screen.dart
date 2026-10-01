import 'package:flutter/material.dart';

import '../models/device.dart';
import '../services/biometric_gate.dart';
import 'connect_screen.dart';

/// Phase 5 - "Session picker optimized for small screens (favorites
/// list, recent connections, search)" + "Offline device list caching".
class DeviceListScreen extends StatefulWidget {
  const DeviceListScreen({super.key});

  @override
  State<DeviceListScreen> createState() => _DeviceListScreenState();
}

class _DeviceListScreenState extends State<DeviceListScreen> {
  final _store = DeviceStore();
  final _biometrics = BiometricGate();
  List<SavedDevice> _devices = [];
  String _query = '';
  bool _biometricLockOn = false;

  @override
  void initState() {
    super.initState();
    _reload();
    _biometrics.isEnabled().then((v) {
      if (mounted) setState(() => _biometricLockOn = v);
    });
  }

  Future<void> _reload() async {
    final devices = await _store.load();
    devices.sort((a, b) => b.lastConnected.compareTo(a.lastConnected));
    if (mounted) setState(() => _devices = devices);
  }

  @override
  Widget build(BuildContext context) {
    final filtered = _devices.where((d) => d.label.toLowerCase().contains(_query.toLowerCase())).toList();
    final favorites = filtered.where((d) => d.favorite).toList();
    final recents = filtered.where((d) => !d.favorite).toList();

    return Scaffold(
      appBar: AppBar(
        title: const Text('Devices'),
        actions: [
          IconButton(
            tooltip: _biometricLockOn ? 'Biometric app lock: on' : 'Biometric app lock: off',
            icon: Icon(_biometricLockOn ? Icons.lock : Icons.lock_open),
            onPressed: () async {
              final supported = await _biometrics.deviceSupportsBiometrics();
              if (!supported && !_biometricLockOn) {
                if (mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('This device has no biometric hardware enrolled.')),
                  );
                }
                return;
              }
              await _biometrics.setEnabled(!_biometricLockOn);
              setState(() => _biometricLockOn = !_biometricLockOn);
            },
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: () async {
          await Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ConnectScreen()));
          _reload();
        },
        child: const Icon(Icons.add),
      ),
      body: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(
          padding: const EdgeInsets.all(8),
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              child: TextField(
                decoration: const InputDecoration(prefixIcon: Icon(Icons.search), hintText: 'Search devices', border: OutlineInputBorder()),
                onChanged: (v) => setState(() => _query = v),
              ),
            ),
            if (favorites.isNotEmpty) _sectionHeader('Favorites'),
            for (final d in favorites) _deviceTile(d),
            if (recents.isNotEmpty) _sectionHeader('Recent'),
            for (final d in recents) _deviceTile(d),
            if (_devices.isEmpty)
              const Padding(
                padding: EdgeInsets.all(32),
                child: Text('No saved devices yet. Tap + to connect to one.', textAlign: TextAlign.center),
              ),
          ],
        ),
      ),
    );
  }

  Widget _sectionHeader(String text) => Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
        child: Text(text, style: Theme.of(context).textTheme.labelLarge),
      );

  Widget _deviceTile(SavedDevice d) {
    return ListTile(
      leading: Icon(d.isRelay ? Icons.cloud_outlined : Icons.lan_outlined),
      title: Text(d.label),
      subtitle: Text(d.isRelay ? 'Relay - ${d.deviceId}' : 'Direct - ${d.host}'),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          IconButton(
            icon: Icon(d.favorite ? Icons.star : Icons.star_border),
            onPressed: () async {
              await _store.setFavorite(d.label, !d.favorite);
              _reload();
            },
          ),
          IconButton(
            icon: const Icon(Icons.delete_outline),
            onPressed: () async {
              await _store.remove(d.label);
              _reload();
            },
          ),
        ],
      ),
      onTap: () async {
        await Navigator.of(context).push(MaterialPageRoute(builder: (_) => ConnectScreen(existing: d)));
        _reload();
      },
    );
  }
}
