import 'package:flutter/material.dart';

import 'screens/device_list_screen.dart';
import 'services/biometric_gate.dart';

void main() {
  runApp(const RemoteControllerApp());
}

class RemoteControllerApp extends StatelessWidget {
  const RemoteControllerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Remote Controller',
      theme: ThemeData(colorSchemeSeed: Colors.indigo, useMaterial3: true),
      darkTheme: ThemeData(colorSchemeSeed: Colors.indigo, brightness: Brightness.dark, useMaterial3: true),
      home: const _AppGate(),
    );
  }
}

/// Phase 5 - biometric app lock. Runs once at launch (and again if the
/// app comes back from the background - see [_onAppResumed] hook point)
/// before the device list or any saved session details are shown.
class _AppGate extends StatefulWidget {
  const _AppGate();

  @override
  State<_AppGate> createState() => _AppGateState();
}

class _AppGateState extends State<_AppGate> {
  final _gate = BiometricGate();
  bool? _unlocked; // null = checking

  @override
  void initState() {
    super.initState();
    _check();
  }

  Future<void> _check() async {
    final unlocked = await _gate.authenticate('Unlock Remote Controller');
    if (mounted) setState(() => _unlocked = unlocked);
  }

  @override
  Widget build(BuildContext context) {
    if (_unlocked == true) return const DeviceListScreen();

    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.fingerprint, size: 64),
            const SizedBox(height: 16),
            if (_unlocked == false) const Text('Authentication failed or was cancelled.'),
            const SizedBox(height: 16),
            FilledButton(onPressed: _check, child: const Text('Unlock')),
          ],
        ),
      ),
    );
  }
}
