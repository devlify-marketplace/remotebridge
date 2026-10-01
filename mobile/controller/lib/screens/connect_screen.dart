import 'package:flutter/material.dart';

import '../models/device.dart';
import '../net/session_client.dart';
import 'remote_control_screen.dart';

/// Manual connection entry - "Manual connection via ID entry" (phase 1)
/// extended with the relay-vs-direct choice, plus save-as-favorite.
class ConnectScreen extends StatefulWidget {
  final SavedDevice? existing;
  const ConnectScreen({super.key, this.existing});

  @override
  State<ConnectScreen> createState() => _ConnectScreenState();
}

class _ConnectScreenState extends State<ConnectScreen> {
  final _formKey = GlobalKey<FormState>();
  bool _useRelay = true;
  final _label = TextEditingController();
  final _host = TextEditingController(text: '192.168.1.23');
  final _videoPort = TextEditingController(text: '5000');
  final _inputPort = TextEditingController(text: '5001');
  final _controlPort = TextEditingController(text: '5002');
  final _relayHost = TextEditingController(text: 'relay.example.com');
  final _relayPort = TextEditingController(text: '6000');
  final _deviceId = TextEditingController();
  final _viewerId = TextEditingController(text: 'phone-viewer');
  final _password = TextEditingController();
  final _totp = TextEditingController();
  bool _saveAsFavorite = false;
  bool _connecting = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final e = widget.existing;
    if (e != null) {
      _label.text = e.label;
      _useRelay = e.isRelay;
      if (e.isRelay) {
        _relayHost.text = e.relayHost ?? '';
        _relayPort.text = '${e.relayPort ?? 6000}';
        _deviceId.text = e.deviceId ?? '';
      } else {
        _host.text = e.host ?? '';
        _videoPort.text = '${e.videoPort ?? 5000}';
        _inputPort.text = '${e.inputPort ?? 5001}';
        _controlPort.text = '${e.controlPort ?? 5002}';
      }
      _saveAsFavorite = e.favorite;
    }
  }

  Future<void> _connect() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _connecting = true;
      _error = null;
    });

    final target = _useRelay
        ? ConnectTarget.relay(_relayHost.text.trim(), int.parse(_relayPort.text.trim()), _deviceId.text.trim())
        : ConnectTarget.direct(
            _host.text.trim(),
            videoPort: int.parse(_videoPort.text.trim()),
            inputPort: int.parse(_inputPort.text.trim()),
            controlPort: int.parse(_controlPort.text.trim()),
          );

    final client = SessionClient(
      target: target,
      viewerId: _viewerId.text.trim(),
      password: _password.text,
      totpCode: _totp.text.trim(),
    );

    try {
      final result = await client.connect();
      if (!result.approved) {
        setState(() {
          _error = 'Host rejected the connection: ${result.reason}';
          _connecting = false;
        });
        return;
      }

      final label = _label.text.trim().isEmpty ? (_useRelay ? _deviceId.text.trim() : _host.text.trim()) : _label.text.trim();

      if (_saveAsFavorite || widget.existing != null) {
        final device = _useRelay
            ? SavedDevice.relay(
                label: label,
                relayHost: _relayHost.text.trim(),
                relayPort: int.parse(_relayPort.text.trim()),
                deviceId: _deviceId.text.trim(),
                favorite: _saveAsFavorite,
              )
            : SavedDevice.direct(
                label: label,
                host: _host.text.trim(),
                videoPort: int.parse(_videoPort.text.trim()),
                inputPort: int.parse(_inputPort.text.trim()),
                controlPort: int.parse(_controlPort.text.trim()),
                favorite: _saveAsFavorite,
              );
        await DeviceStore().upsert(device);
      } else {
        await DeviceStore().touchLastConnected(label);
      }

      if (!mounted) return;
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => RemoteControlScreen(session: client, deviceLabel: label)),
      );
    } catch (e) {
      setState(() {
        _error = 'Could not connect: $e';
        _connecting = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Connect')),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            TextFormField(controller: _label, decoration: const InputDecoration(labelText: 'Label (optional)')),
            const SizedBox(height: 12),
            SegmentedButton<bool>(
              segments: const [
                ButtonSegment(value: true, label: Text('Relay (by ID)')),
                ButtonSegment(value: false, label: Text('Direct (LAN IP)')),
              ],
              selected: {_useRelay},
              onSelectionChanged: (s) => setState(() => _useRelay = s.first),
            ),
            const SizedBox(height: 12),
            if (_useRelay) ...[
              TextFormField(controller: _relayHost, decoration: const InputDecoration(labelText: 'Relay host'), validator: _req),
              TextFormField(controller: _relayPort, decoration: const InputDecoration(labelText: 'Relay port'), keyboardType: TextInputType.number, validator: _req),
              TextFormField(controller: _deviceId, decoration: const InputDecoration(labelText: 'Device ID'), validator: _req),
            ] else ...[
              TextFormField(controller: _host, decoration: const InputDecoration(labelText: 'Host IP'), validator: _req),
              TextFormField(controller: _videoPort, decoration: const InputDecoration(labelText: 'Video port'), keyboardType: TextInputType.number, validator: _req),
              TextFormField(controller: _inputPort, decoration: const InputDecoration(labelText: 'Input port'), keyboardType: TextInputType.number, validator: _req),
              TextFormField(controller: _controlPort, decoration: const InputDecoration(labelText: 'Control port'), keyboardType: TextInputType.number, validator: _req),
            ],
            const Divider(height: 32),
            TextFormField(controller: _viewerId, decoration: const InputDecoration(labelText: 'Your viewer ID'), validator: _req),
            TextFormField(
              controller: _password,
              decoration: const InputDecoration(labelText: 'Unattended password (if set on host)'),
              obscureText: true,
            ),
            TextFormField(
              controller: _totp,
              decoration: const InputDecoration(labelText: '2FA code (if enabled)'),
              keyboardType: TextInputType.number,
            ),
            SwitchListTile(
              title: const Text('Save to favorites'),
              value: _saveAsFavorite,
              onChanged: (v) => setState(() => _saveAsFavorite = v),
            ),
            if (_error != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error))),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: _connecting ? null : _connect,
              child: _connecting ? const CircularProgressIndicator() : const Text('Connect'),
            ),
          ],
        ),
      ),
    );
  }

  String? _req(String? v) => (v == null || v.trim().isEmpty) ? 'Required' : null;
}
