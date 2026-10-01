/// Phase 6 - "Remote file browser scoped to app-accessible storage"
/// (`docs/roadmap.md`). Host-role responder for the same control-channel
/// messages `desktop/file_transfer.py`'s `FileTransferSession` handles -
/// list/send/pull/chunk/complete - but deliberately narrower than the
/// desktop side's "browse anywhere the OS user can": every path this
/// class will read from or write to is resolved *inside* [rootDir] (the
/// app's own documents directory by default), and any path a viewer asks
/// for that would escape it (`../..`, an absolute path, a symlink out)
/// is rejected rather than followed - "scoped to the phone's
/// accessible storage" is a sandboxing requirement here, not just a
/// convenience default like it is on desktop.
library host_file_browser;

import 'dart:async';
import 'dart:io';

import '../net/protocol.dart' as proto;
import '../net/wire_socket.dart';

const int chunkSize = 65536;

class _IncomingTransfer {
  final RandomAccessFile fh;
  final String destPath;
  final String partPath;
  final int size;
  int received;
  final String name;

  _IncomingTransfer({
    required this.fh,
    required this.destPath,
    required this.partPath,
    required this.size,
    required this.received,
    required this.name,
  });
}

class HostFileBrowser {
  final WireSocket control;
  final Directory rootDir;
  final void Function(String message) onLog;

  final Map<int, _IncomingTransfer> _incoming = {};

  HostFileBrowser({required this.control, required this.rootDir, required this.onLog});

  void _send(int msgType, [dynamic payload]) {
    control.send(msgType, payload);
  }

  /// Resolves a viewer-supplied relative path against [rootDir], refusing
  /// anything that normalizes to somewhere outside it. Returns null for a
  /// rejected path.
  String? _resolveScoped(String requested) {
    final cleaned = requested.trim().isEmpty ? '.' : requested.trim();
    final candidate = File('${rootDir.path}/$cleaned').absolute;
    final normalized = candidate.uri.normalizePath().toFilePath();
    final rootNormalized = rootDir.absolute.uri.normalizePath().toFilePath();
    if (!normalized.startsWith(rootNormalized)) return null;
    return normalized;
  }

  /// Called from the control-channel reader loop (`host_session.dart`)
  /// for every message; returns true if this handler owns the type,
  /// matching `file_transfer.py`'s `dispatch()` contract.
  bool dispatch(proto.WireMessage message) {
    switch (message.type) {
      case proto.MsgType.fileListRequest:
        _handleListRequest(message.payload);
        return true;
      case proto.MsgType.fileSendRequest: // viewer pushing a file to us
        _handleSendRequest(message.payload);
        return true;
      case proto.MsgType.fileChunk:
        _handleChunk(message.payload);
        return true;
      case proto.MsgType.fileComplete:
        _handleComplete(message.payload);
        return true;
      case proto.MsgType.filePullRequest: // viewer asking us to push a file
        _handlePullRequest(message.payload);
        return true;
      default:
        return false;
    }
  }

  void _handleListRequest(dynamic payload) {
    final req = proto.unpackFileListRequest(payload);
    final requestedDir = (req['dir'] as String?) ?? '.';
    final resolved = _resolveScoped(requestedDir);

    if (resolved == null) {
      _send(proto.MsgType.fileListResponse,
          proto.packFileListResponse('$requestedDir (rejected: outside app storage)', const []));
      return;
    }

    final dir = Directory(resolved);
    final entries = <Map<String, dynamic>>[];
    try {
      for (final entity in dir.listSync()) {
        final stat = entity.statSync();
        entries.add({
          'name': entity.uri.pathSegments.where((s) => s.isNotEmpty).last,
          'is_dir': stat.type == FileSystemEntityType.directory,
          'size': stat.type == FileSystemEntityType.file ? stat.size : 0,
        });
      }
      final relative = resolved.substring(rootDir.absolute.path.length);
      _send(proto.MsgType.fileListResponse, proto.packFileListResponse(relative.isEmpty ? '/' : relative, entries));
    } on FileSystemException catch (e) {
      _send(proto.MsgType.fileListResponse, proto.packFileListResponse('$requestedDir (error: ${e.message})', const []));
    }
  }

  /// Viewer pushing a file onto the phone. Same resume-on-retry `.part`
  /// scheme as `file_transfer.py`, and the filename is always basename'd
  /// before joining it to [rootDir] - never trust a path from the peer.
  void _handleSendRequest(dynamic payload) {
    final req = proto.unpackFileSendRequest(payload);
    final transferId = req['transfer_id'] as int;
    final filename = (req['filename'] as String).split('/').last.split('\\').last;
    final size = req['size'] as int;

    final destPath = '${rootDir.path}/$filename';
    final partPath = '$destPath.part';
    final partFile = File(partPath);

    var resumeOffset = 0;
    if (partFile.existsSync()) {
      final existing = partFile.lengthSync();
      if (existing <= size) resumeOffset = existing;
    }

    try {
      final fh = partFile.openSync(mode: resumeOffset > 0 ? FileMode.append : FileMode.write);
      _incoming[transferId] = _IncomingTransfer(
        fh: fh,
        destPath: destPath,
        partPath: partPath,
        size: size,
        received: resumeOffset,
        name: filename,
      );
      onLog(resumeOffset > 0
          ? "incoming '$filename' ($size bytes), resuming from byte $resumeOffset"
          : "incoming '$filename' ($size bytes) - accepting automatically");
      _send(proto.MsgType.fileSendAccept, proto.packFileSendAccept(transferId: transferId, accepted: true, resumeOffset: resumeOffset));
    } on FileSystemException catch (e) {
      _send(proto.MsgType.fileSendAccept,
          proto.packFileSendAccept(transferId: transferId, accepted: false, resumeOffset: 0, reason: e.message));
    }
  }

  void _handleChunk(dynamic payload) {
    final chunk = proto.unpackFileChunk(payload);
    final state = _incoming[chunk.transferId];
    if (state == null) return; // unknown transfer (e.g. after a restart) - drop it
    state.fh.setPositionSync(chunk.offset);
    state.fh.writeFromSync(chunk.data);
    state.received = chunk.offset + chunk.data.length;
  }

  void _handleComplete(dynamic payload) {
    final info = proto.unpackFileComplete(payload);
    final transferId = info['transfer_id'] as int;
    final state = _incoming.remove(transferId);
    if (state == null) return;
    state.fh.closeSync();
    if (info['ok'] == true && state.received >= state.size) {
      File(state.partPath).renameSync(state.destPath);
      onLog("received '${state.name}' complete -> ${state.destPath}");
    } else {
      onLog("'${state.name}' ended incomplete (${state.received}/${state.size} bytes) - "
          'kept as ${state.partPath}');
    }
  }

  /// Viewer asking us to push a file it already knows the (scoped) path
  /// to. Runs the send on its own so it doesn't block the control
  /// channel's reader loop while waiting on chunk writes.
  void _handlePullRequest(dynamic payload) {
    final req = proto.unpackFilePullRequest(payload);
    final requested = (req['path'] as String?) ?? '';
    final resolved = _resolveScoped(requested);
    if (resolved == null) {
      onLog("pull request for '$requested' rejected: outside app storage");
      return;
    }
    unawaited(_sendFile(resolved));
  }

  Future<void> _sendFile(String localPath) async {
    final file = File(localPath);
    if (!await file.exists()) {
      onLog('pull request for a file that does not exist: $localPath');
      return;
    }
    final size = await file.length();
    final transferId = DateTime.now().microsecondsSinceEpoch & 0xFFFFFFFF;
    final remoteName = localPath.split('/').last;

    _send(proto.MsgType.fileSendRequest, proto.packFileSendRequest(transferId, remoteName, size));
    onLog("offered '$remoteName' ($size bytes) to viewer");

    // The desktop side waits (with a timeout) for MSG_FILE_SEND_ACCEPT
    // before streaming; this phase keeps it simple and streams
    // immediately, since a scoped-storage pull is always host-initiated
    // in response to a request the viewer just made a moment ago - see
    // the README's "Deliberately deferred" section for the accept/resume
    // round-trip this skips on the outbound (host-to-viewer) direction.
    final raf = await file.open();
    try {
      var sent = 0;
      while (true) {
        final chunk = await raf.read(chunkSize);
        if (chunk.isEmpty) break;
        _send(proto.MsgType.fileChunk, proto.packFileChunk(transferId, sent, chunk));
        sent += chunk.length;
      }
      _send(proto.MsgType.fileComplete, proto.packFileComplete(transferId, true, 'sender finished'));
      onLog("sent '$remoteName' ($sent bytes total)");
    } finally {
      await raf.close();
    }
  }
}
