import 'package:flutter/material.dart';

import 'screens/host_setup_screen.dart';

void main() {
  runApp(const RemoteHostApp());
}

class RemoteHostApp extends StatelessWidget {
  const RemoteHostApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RemoteBridge Host',
      theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
      darkTheme: ThemeData(colorSchemeSeed: Colors.teal, brightness: Brightness.dark, useMaterial3: true),
      home: const HostSetupScreen(),
    );
  }
}
