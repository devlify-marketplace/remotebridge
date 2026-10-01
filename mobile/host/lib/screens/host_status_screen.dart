/// Phase 6 - runs a `HostSession` for as long as this screen stays open
/// and surfaces what `host.py`'s own console log line printed on the
/// desktop side: connection state, who's connected, the combined
/// adaptive-bitrate/battery-throttle readout, and the same "who
/// connected, when, for how long" record `session_log.dart` keeps
/// (Phase 2's `sessions.log`, read back via `recentEvents()`).
///
/// Known limitation: `HostSession.requestStop()` only breaks the outer
/// `while (!_stopRequested)` loop between sessions - if the app is
/// currently blocked waiting for the *next* viewer to connect (awaiting
/// a socket accept / relay registration), leaving this screen won't
/// interrupt that wait early. Not fixed in this phase; see the README.
library host_status_screen;

import 'dart:async';

import 'package:flutter/material.dart';

import '../models/host_config.dart';
import '../net/host_session.dart';
import '../services/session_log.dart' as session_log;
import '../widgets/session_confirmation_dialog.dart';

class HostStatusScreen extends StatefulWidget {
  final HostTarget target;
  final HostConfig config;

  const HostStatusScreen({super.key, required this.target, required this.config});

  @override
  State<HostStatusScreen> createState() => _HostStatusScreenState();
}

class _HostStatusScreenState extends State<HostStatusScreen> {
  late final HostSession _session;
  final List<String> _log = [];
  List<Map<String, dynamic>> _recent = [];
  Timer? _refreshTimer;
  String? _activeViewer;

  @override
  void initState() {
    super.initState();
    _session = HostSession(
      target: widget.target,
      config: widget.config,
      onLog: _appendLog,
      onConfirmationNeeded: (viewerId, address, timeoutSeconds) => SessionConfirmationDialog.show(
        context,
        viewerId: viewerId,
        address: address,
        timeoutSeconds: timeoutSeconds,
      ),
      onSessionEnded: (viewerId, duration) {
        if (!mounted) return;
        setState(() => _activeViewer = null);
        unawaited(_reloadRecent());
      },
    );
    unawaited(_session.run());
    unawaited(_reloadRecent());
    // The battery/bitrate readout below changes on events this screen
    // doesn't otherwise listen to (battery ticks, per-frame bitrate
    // steps) - a light periodic rebuild keeps it current without wiring
    // a dedicated stream through HostSession for what's just a status
    // display.
    _refreshTimer = Timer.periodic(const Duration(seconds: 2), (_) {
      if (mounted) setState(() {});
    });
  }

  void _appendLog(String message) {
    if (!mounted) return;
    setState(() {
      _log.insert(0, message);
      if (_log.length > 200) _log.removeRange(200, _log.length);
      if (message.startsWith('connection approved')) {
        final match = RegExp(r"viewer '([^']+)'").firstMatch(message);
        if (match != null) _activeViewer = match.group(1);
      }
    });
  }

  Future<void> _reloadRecent() async {
    final events = await session_log.recentEvents(limit: 20);
    if (mounted) setState(() => _recent = events);
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _session.requestStop();
    unawaited(_session.capture.stop().catchError((_) {}));
    unawaited(_session.batteryGuard.stop());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final bitrate = _session.bitrate.settings;
    final tier = _session.batteryGuard.currentTier;
    final ended = _recent.where((e) => e['event'] == 'end').toList();

    return Scaffold(
      appBar: AppBar(
        title: const Text('Hosting'),
        actions: [
          IconButton(
            tooltip: 'Stop hosting',
            icon: const Icon(Icons.stop_circle_outlined),
            onPressed: () {
              _session.requestStop();
              Navigator.of(context).pop();
            },
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(
                        _activeViewer != null ? Icons.screen_share : Icons.stop_screen_share_outlined,
                        color: _activeViewer != null ? Colors.green : null,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          _activeViewer != null ? 'Viewer connected: $_activeViewer' : 'Waiting for a viewer...',
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    widget.target.isRelay
                        ? 'Relay: ${widget.target.deviceId}'
                        : 'Direct - video:${widget.target.videoPort}  '
                            'input:${widget.target.inputPort}  control:${widget.target.controlPort}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const Divider(height: 24),
                  Text('Quality ${bitrate.quality} · ${bitrate.fps} fps'),
                  Text('Battery: ${_session.batteryGuard.levelPercent}% - ${tier.label}'),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          Text('Recent connections', style: Theme.of(context).textTheme.titleMedium),
          if (ended.isEmpty)
            const Padding(padding: EdgeInsets.symmetric(vertical: 8), child: Text('No sessions recorded yet.'))
          else
            for (final e in ended)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.history),
                title: Text('${e['viewer_id']}'),
                subtitle: Text(
                  '${e['timestamp']} · ${(e['duration_seconds'] as num?)?.toStringAsFixed(0) ?? '?'}s',
                ),
              ),
          const Divider(height: 32),
          Text('Log', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          for (final line in _log.take(50))
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Text(line, style: Theme.of(context).textTheme.bodySmall),
            ),
        ],
      ),
    );
  }
}
