/// Dart port of `desktop/session_log.py` - same newline-delimited-JSON
/// format (`{"event": ..., "timestamp": ..., ...}` per line) appended to
/// `sessions.log`, so the "who connected, when, for how long" record this
/// phase adds is in the same shape as the desktop host's, just written to
/// the app's documents directory instead of the working directory a CLI
/// process happens to be run from.
library session_log;

import 'dart:convert';
import 'dart:io';

import 'package:path_provider/path_provider.dart';

const String logFileName = 'sessions.log';

Future<File> _logFile() async {
  final dir = await getApplicationDocumentsDirectory();
  return File('${dir.path}/$logFileName');
}

Future<void> logEvent(String event, Map<String, dynamic> fields) async {
  final entry = {
    'event': event,
    'timestamp': DateTime.now().toUtc().toIso8601String(),
    ...fields,
  };
  final file = await _logFile();
  await file.writeAsString('${jsonEncode(entry)}\n', mode: FileMode.append, flush: true);
}

/// Reads back the most recent [limit] log lines, newest first, for the
/// host status screen's "recent connections" list.
Future<List<Map<String, dynamic>>> recentEvents({int limit = 50}) async {
  final file = await _logFile();
  if (!await file.exists()) return [];
  final lines = (await file.readAsLines()).where((l) => l.trim().isNotEmpty).toList();
  final tail = lines.length > limit ? lines.sublist(lines.length - limit) : lines;
  return tail.reversed.map((l) => jsonDecode(l) as Map<String, dynamic>).toList();
}

/// Mirrors `session_log.py`'s `SessionTimer` - wall-clock duration from
/// construction to `elapsed()`.
class SessionTimer {
  final Stopwatch _stopwatch = Stopwatch()..start();

  double elapsedSeconds() => _stopwatch.elapsedMilliseconds / 1000.0;
}
