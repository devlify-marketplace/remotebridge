/// Phase 6 host setup - the mirror of `mobile/controller`'s
/// `connect_screen.dart`, but for the side that will be listened *into*
/// rather than dial *out*: picks one of `host_session.dart`'s two
/// `HostTarget` modes (direct LAN ports, matching `host.py
/// --video-port ...`, or a relay device ID, matching `host.py --relay
/// ... --id ...`), and exposes the Phase 2 access-control knobs this
/// phase actually ports to a phone - unattended password,
/// prompt-on-unrecognized-viewer, and its timeout. TOTP and whitelist
/// are deliberately not surfaced here - see this app's README.
library host_setup_screen;

import 'package:flutter/material.dart';

import '../models/host_config.dart';
import '../net/host_session.dart';
import 'host_status_screen.dart';

class HostSetupScreen extends StatefulWidget {
  const HostSetupScreen({super.key});

  @override
  State<HostSetupScreen> createState() => _HostSetupScreenState();
}

class _HostSetupScreenState extends State<HostSetupScreen> {
  final _formKey = GlobalKey<FormState>();
  HostConfig? _config;
  bool _loading = true;

  bool _useRelay = false;
  final _videoPort = TextEditingController(text: '5000');
  final _inputPort = TextEditingController(text: '5001');
  final _controlPort = TextEditingController(text: '5002');
  final _relayHost = TextEditingController(text: 'relay.example.com');
  final _relayPort = TextEditingController(text: '6000');
  final _deviceId = TextEditingController(text: 'my-phone');
  final _relayToken = TextEditingController();

  final _newPassword = TextEditingController();
  bool _showPasswordField = false;

  static const _timeoutChoices = [10, 20, 30, 60];

  @override
  void initState() {
    super.initState();
    HostConfig.load().then((c) {
      if (!mounted) return;
      setState(() {
        _config = c;
        _loading = false;
      });
    });
  }

  @override
  void dispose() {
    _videoPort.dispose();
    _inputPort.dispose();
    _controlPort.dispose();
    _relayHost.dispose();
    _relayPort.dispose();
    _deviceId.dispose();
    _relayToken.dispose();
    _newPassword.dispose();
    super.dispose();
  }

  Future<void> _setPassword() async {
    if (_newPassword.text.isEmpty) return;
    _config!.setUnattendedPassword(_newPassword.text);
    await _config!.save();
    _newPassword.clear();
    if (mounted) setState(() => _showPasswordField = false);
  }

  Future<void> _clearPassword() async {
    _config!.clearUnattendedPassword();
    await _config!.save();
    if (mounted) setState(() {});
  }

  void _startHosting() {
    if (!_formKey.currentState!.validate()) return;

    final target = _useRelay
        ? HostTarget.relay(
            relayHost: _relayHost.text.trim(),
            relayPort: int.parse(_relayPort.text.trim()),
            deviceId: _deviceId.text.trim(),
            relayToken: _relayToken.text.trim(),
          )
        : HostTarget.direct(
            videoPort: int.parse(_videoPort.text.trim()),
            inputPort: int.parse(_inputPort.text.trim()),
            controlPort: int.parse(_controlPort.text.trim()),
          );

    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => HostStatusScreen(target: target, config: _config!)),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final config = _config!;

    return Scaffold(
      appBar: AppBar(title: const Text('Host This Phone')),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text('Connection', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            SegmentedButton<bool>(
              segments: const [
                ButtonSegment(value: true, label: Text('Relay (by ID)')),
                ButtonSegment(value: false, label: Text('Direct (LAN)')),
              ],
              selected: {_useRelay},
              onSelectionChanged: (s) => setState(() => _useRelay = s.first),
            ),
            const SizedBox(height: 12),
            if (_useRelay) ...[
              TextFormField(
                controller: _relayHost,
                decoration: const InputDecoration(labelText: 'Relay host'),
                validator: _req,
              ),
              TextFormField(
                controller: _relayPort,
                decoration: const InputDecoration(labelText: 'Relay port'),
                keyboardType: TextInputType.number,
                validator: _req,
              ),
              TextFormField(
                controller: _deviceId,
                decoration: const InputDecoration(labelText: 'Device ID to register as'),
                validator: _req,
              ),
              TextFormField(
                controller: _relayToken,
                decoration: const InputDecoration(
                    labelText: 'Relay token (only if the relay requires one)'),
                obscureText: true,
              ),
            ] else ...[
              TextFormField(
                controller: _videoPort,
                decoration: const InputDecoration(labelText: 'Video port'),
                keyboardType: TextInputType.number,
                validator: _req,
              ),
              TextFormField(
                controller: _inputPort,
                decoration: const InputDecoration(labelText: 'Input port'),
                keyboardType: TextInputType.number,
                validator: _req,
              ),
              TextFormField(
                controller: _controlPort,
                decoration: const InputDecoration(labelText: 'Control port'),
                keyboardType: TextInputType.number,
                validator: _req,
              ),
            ],
            const Divider(height: 32),
            Text('Access control', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            if (config.hasUnattendedPassword)
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.lock_outline),
                title: const Text('Unattended password set'),
                subtitle: const Text('A viewer presenting it connects automatically'),
                trailing: TextButton(onPressed: _clearPassword, child: const Text('Clear')),
              )
            else if (_showPasswordField)
              Row(
                crossAxisAlignment: CrossAxisAlignment.center,
                children: [
                  Expanded(
                    child: TextFormField(
                      controller: _newPassword,
                      obscureText: true,
                      decoration: const InputDecoration(labelText: 'New unattended password'),
                    ),
                  ),
                  IconButton(icon: const Icon(Icons.check), onPressed: _setPassword),
                ],
              )
            else
              OutlinedButton.icon(
                onPressed: () => setState(() => _showPasswordField = true),
                icon: const Icon(Icons.add),
                label: const Text('Set unattended password'),
              ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Prompt on unrecognized viewer'),
              subtitle: const Text('Show an accept/reject dialog when no valid password is presented'),
              value: config.promptOnUnrecognizedViewer,
              onChanged: (v) async {
                config.promptOnUnrecognizedViewer = v;
                await config.save();
                if (mounted) setState(() {});
              },
            ),
            Row(
              children: [
                const Text('Confirmation timeout'),
                const Spacer(),
                DropdownButton<int>(
                  value: config.confirmationTimeoutSeconds,
                  items: _timeoutChoices
                      .map((s) => DropdownMenuItem(value: s, child: Text('${s}s')))
                      .toList(),
                  onChanged: (v) async {
                    if (v == null) return;
                    config.confirmationTimeoutSeconds = v;
                    await config.save();
                    if (mounted) setState(() {});
                  },
                ),
              ],
            ),
            if (!config.hasUnattendedPassword && !config.promptOnUnrecognizedViewer)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'No password set and prompting is off - every connection attempt will '
                  'be auto-rejected until you change one of these.',
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: _startHosting,
              icon: const Icon(Icons.play_arrow),
              label: const Text('Start Hosting'),
            ),
          ],
        ),
      ),
    );
  }

  String? _req(String? v) => (v == null || v.trim().isEmpty) ? 'Required' : null;
}
