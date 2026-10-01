import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show DeviceOrientation;

import '../net/session_client.dart';
import '../services/gesture_engine.dart';
import '../services/orientation_service.dart';
import '../widgets/onscreen_keyboard.dart';

class RemoteControlScreen extends StatefulWidget {
  final SessionClient session;
  final String deviceLabel;

  const RemoteControlScreen({super.key, required this.session, required this.deviceLabel});

  @override
  State<RemoteControlScreen> createState() => _RemoteControlScreenState();
}

class _RemoteControlScreenState extends State<RemoteControlScreen> {
  Uint8List? _latestFrame;
  bool _showKeyboard = false;
  late GestureEngine _gestures;
  final _orientation = OrientationService();
  List<dynamic> _monitors = [];
  int _activeMonitor = 0;

  @override
  void initState() {
    super.initState();
    _gestures = GestureEngine(session: widget.session, viewportSize: Size.zero);
    _gestures.onViewTransformChanged = () => setState(() {});

    widget.session.frames.listen((bytes) {
      if (mounted) setState(() => _latestFrame = bytes);
    });

    widget.session.monitorUpdates.listen((update) {
      if (!mounted) return;
      setState(() {
        _monitors = (update['monitors'] as List<dynamic>?) ?? [];
        _activeMonitor = update['active'] as int? ?? 0;
      });
      if (_monitors.isNotEmpty) {
        final active = _monitors.firstWhere(
          (m) => (m as Map)['index'] == _activeMonitor,
          orElse: () => _monitors.first,
        ) as Map;
        final w = active['width'] as int? ?? 1920;
        final h = active['height'] as int? ?? 1080;
        _orientation.matchRemoteAspect(w, h);
      }
    });
  }

  @override
  void dispose() {
    widget.session.disconnect();
    _orientation.unlock();
    super.dispose();
  }

  void _toggleMode() {
    setState(() {
      _gestures.mode = _gestures.mode == InputMode.directTouch ? InputMode.trackpad : InputMode.directTouch;
    });
  }

  void _toggleLowData() {
    setState(() => widget.session.lowDataMode = !widget.session.lowDataMode);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        title: Text(widget.deviceLabel),
        actions: [
          IconButton(
            tooltip: widget.session.lowDataMode ? 'Low-data mode: on' : 'Low-data mode: off',
            icon: Icon(widget.session.lowDataMode ? Icons.data_saver_on : Icons.data_saver_off),
            onPressed: _toggleLowData,
          ),
          IconButton(
            tooltip: _gestures.mode == InputMode.directTouch ? 'Switch to trackpad mode' : 'Switch to direct-touch mode',
            icon: Icon(_gestures.mode == InputMode.directTouch ? Icons.touch_app : Icons.mouse),
            onPressed: _toggleMode,
          ),
          if (_monitors.length > 1)
            PopupMenuButton<int>(
              icon: const Icon(Icons.monitor),
              onSelected: (i) => widget.session.switchMonitor(i),
              itemBuilder: (context) => [
                for (final m in _monitors)
                  PopupMenuItem(value: (m as Map)['index'] as int, child: Text('Monitor ${m['index']}')),
              ],
            ),
          IconButton(
            tooltip: _orientation.isLocked ? 'Unlock orientation' : 'Lock orientation',
            icon: Icon(_orientation.isLocked ? Icons.screen_lock_rotation : Icons.screen_rotation),
            onPressed: () async {
              if (_orientation.isLocked) {
                await _orientation.unlock();
              } else {
                await _orientation.lockTo(MediaQuery.of(context).orientation == Orientation.landscape
                    ? DeviceOrientation.landscapeLeft
                    : DeviceOrientation.portraitUp);
              }
              setState(() {});
            },
          ),
          IconButton(
            tooltip: _showKeyboard ? 'Hide keyboard' : 'Show keyboard',
            icon: Icon(_showKeyboard ? Icons.keyboard_hide : Icons.keyboard),
            onPressed: () => setState(() => _showKeyboard = !_showKeyboard),
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: LayoutBuilder(
              builder: (context, constraints) {
                _gestures.viewportSize = Size(constraints.maxWidth, constraints.maxHeight);
                return Listener(
                  onPointerDown: (e) => _gestures.onPointerDown(e.pointer, e.localPosition),
                  onPointerMove: (e) => _gestures.onPointerMove(e.pointer, e.localPosition, e.delta),
                  onPointerUp: (e) => _gestures.onPointerUp(e.pointer, e.localPosition),
                  onPointerCancel: (e) => _gestures.onPointerCancel(e.pointer),
                  child: GestureDetector(
                    // Double-tap to reset the local pinch-zoom back to
                    // 1:1 - the pinch gesture itself is handled by the
                    // Listener above, not by this detector.
                    onDoubleTap: () => setState(_gestures.resetZoom),
                    child: ClipRect(
                      child: Transform(
                        alignment: Alignment.center,
                        transform: Matrix4.diagonal3Values(_gestures.zoom, _gestures.zoom, 1.0),
                        child: Center(
                          child: _latestFrame == null
                              ? const CircularProgressIndicator()
                              : Image.memory(
                                  _latestFrame!,
                                  gaplessPlayback: true,
                                  fit: BoxFit.contain,
                                ),
                        ),
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
          if (_showKeyboard) OnScreenKeyboard(session: widget.session),
        ],
      ),
    );
  }
}
