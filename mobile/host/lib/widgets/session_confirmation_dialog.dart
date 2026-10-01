/// Phase 2 - "Session confirmation prompt (accept/reject incoming
/// connection)", the mobile-host counterpart of the desktop side's
/// blocking console `input()` prompt (`desktop/auth.py`'s
/// `prompt_accept`). Shown from `host_status_screen.dart` by passing
/// [SessionConfirmationDialog.show] as `HostSession.onConfirmationNeeded`
/// - see `net/host_session.dart`'s `_decide()`, which calls it whenever a
/// viewer connects with no valid unattended password.
///
/// Rejects by default on timeout, same reasoning as the desktop side's
/// `prompt_accept`: there's no guarantee anyone is actually looking at
/// the phone right now, so silence should mean "no", not "yes".
library session_confirmation_dialog;

import 'dart:async';

import 'package:flutter/material.dart';

class SessionConfirmationDialog {
  SessionConfirmationDialog._();

  /// Shows the dialog and resolves once the user taps Accept/Reject, or
  /// after [timeoutSeconds] elapses with no answer (resolves to false).
  /// If [context] is no longer mounted when called - e.g. the status
  /// screen was popped between the viewer connecting and the auth
  /// handshake completing - resolves to false without showing anything,
  /// rather than throwing.
  static Future<bool> show(
    BuildContext context, {
    required String viewerId,
    required String address,
    required int timeoutSeconds,
  }) async {
    if (!context.mounted) return false;
    final result = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (_) => _ConfirmationDialogBody(
        viewerId: viewerId,
        address: address,
        timeoutSeconds: timeoutSeconds,
      ),
    );
    // A null result means the dialog was dismissed some other way (e.g.
    // the route was popped out from under it) rather than an explicit
    // button tap - treat that as a reject too, not an accept.
    return result ?? false;
  }
}

class _ConfirmationDialogBody extends StatefulWidget {
  final String viewerId;
  final String address;
  final int timeoutSeconds;

  const _ConfirmationDialogBody({
    required this.viewerId,
    required this.address,
    required this.timeoutSeconds,
  });

  @override
  State<_ConfirmationDialogBody> createState() => _ConfirmationDialogBodyState();
}

class _ConfirmationDialogBodyState extends State<_ConfirmationDialogBody> {
  late int _secondsLeft = widget.timeoutSeconds;
  Timer? _ticker;

  @override
  void initState() {
    super.initState();
    if (_secondsLeft <= 0) return;
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      setState(() => _secondsLeft -= 1);
      if (_secondsLeft <= 0) {
        _ticker?.cancel();
        if (Navigator.of(context).canPop()) {
          Navigator.of(context).pop(false);
        }
      }
    });
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      icon: const Icon(Icons.screen_share_outlined, size: 32),
      title: const Text('Incoming connection'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _row('Viewer ID', widget.viewerId),
          _row('From', widget.address),
          const SizedBox(height: 12),
          Text(
            'No unattended password was presented for this connection. '
            'Auto-rejecting in $_secondsLeft s.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Reject'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Accept'),
        ),
      ],
    );
  }

  Widget _row(String label, String value) => Padding(
        padding: const EdgeInsets.only(bottom: 4),
        child: RichText(
          text: TextSpan(
            style: DefaultTextStyle.of(context).style,
            children: [
              TextSpan(text: '$label: ', style: const TextStyle(fontWeight: FontWeight.w600)),
              TextSpan(text: value),
            ],
          ),
        ),
      );
}
