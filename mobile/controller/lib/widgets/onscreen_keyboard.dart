import 'package:flutter/material.dart';

import '../net/session_client.dart';

/// Phase 5 - "On-screen keyboard with modifier/function keys".
///
/// Two pieces:
///  1. A normal text field. Flutter's own keyboard handles letters,
///     numbers, punctuation; each character typed is sent as a
///     press+release key event immediately (see [_TextRelay]) and the
///     field is cleared right after, so it never accumulates local text
///     the person has to manage.
///  2. A pinned row of keys a phone keyboard doesn't have at all -
///     modifiers that need to be held (not tapped) plus Esc/F1-F12 -
///     with toggle buttons for the modifiers so e.g. Ctrl+C works as
///     "toggle Ctrl, tap C" instead of needing multi-touch chording.
class OnScreenKeyboard extends StatefulWidget {
  final SessionClient session;
  const OnScreenKeyboard({super.key, required this.session});

  @override
  State<OnScreenKeyboard> createState() => _OnScreenKeyboardState();
}

class _OnScreenKeyboardState extends State<OnScreenKeyboard> {
  final _controller = TextEditingController();
  final Set<String> _heldModifiers = {}; // e.g. "ctrl_l", "alt_l", "shift", "cmd"
  bool _showFRow = false;

  static const _modifiers = [
    ('Ctrl', 'ctrl_l'),
    ('Alt', 'alt_l'),
    ('Shift', 'shift'),
    ('Cmd/Win', 'cmd'),
  ];

  static const _fRow = [
    'esc', 'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12',
    'tab', 'delete',
  ];

  void _toggleModifier(String key) {
    setState(() {
      if (_heldModifiers.contains(key)) {
        widget.session.sendKey(key, false);
        _heldModifiers.remove(key);
      } else {
        widget.session.sendKey(key, true);
        _heldModifiers.add(key);
      }
    });
  }

  void _tapNamedKey(String key) {
    // A modifier held via the row above should combine with this tap
    // (e.g. Ctrl held + tapping "c") rather than the tap releasing it.
    widget.session.sendKey(key, true);
    widget.session.sendKey(key, false);
  }

  void _releaseAllModifiersAfterCombo() {
    // Most remote shortcuts (Ctrl+C, Alt+Tab, Cmd+Space) are meant as a
    // single combo, not a sticky modifier for everything typed after.
    // Auto-release once a named-key tap has gone through.
    for (final key in _heldModifiers.toList()) {
      widget.session.sendKey(key, false);
    }
    setState(() => _heldModifiers.clear());
  }

  void _onTextChanged(String value) {
    if (value.isEmpty) return;
    // Plain substring rather than the `characters` package's grapheme-
    // aware split - good enough for the ASCII/Latin key names the host
    // side (pynput) understands; multi-codepoint emoji aren't a
    // meaningful "key" to forward anyway.
    final ch = value.substring(value.length - 1);
    widget.session.sendKey(ch, true);
    widget.session.sendKey(ch, false);
    _controller.clear();
  }

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      child: SafeArea(
        top: false,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              child: Row(
                children: [
                  for (final (label, key) in _modifiers)
                    Padding(
                      padding: const EdgeInsets.only(right: 6),
                      child: FilterChip(
                        label: Text(label),
                        selected: _heldModifiers.contains(key),
                        onSelected: (_) => _toggleModifier(key),
                      ),
                    ),
                  const SizedBox(width: 8),
                  IconButton(
                    tooltip: _showFRow ? 'Hide function keys' : 'Show Esc / F1-F12',
                    icon: Icon(_showFRow ? Icons.expand_less : Icons.expand_more),
                    onPressed: () => setState(() => _showFRow = !_showFRow),
                  ),
                ],
              ),
            ),
            if (_showFRow)
              SizedBox(
                height: 44,
                child: ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  children: [
                    for (final key in _fRow)
                      Padding(
                        padding: const EdgeInsets.only(right: 6),
                        child: OutlinedButton(
                          onPressed: () {
                            _tapNamedKey(key);
                            if (_heldModifiers.isNotEmpty) _releaseAllModifiersAfterCombo();
                          },
                          child: Text(key.toUpperCase()),
                        ),
                      ),
                  ],
                ),
              ),
            Row(
              children: [
                Expanded(
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                    child: TextField(
                      controller: _controller,
                      autofocus: true,
                      decoration: const InputDecoration(
                        isDense: true,
                        border: OutlineInputBorder(),
                        hintText: 'Type - sent to the remote host as key events',
                      ),
                      onChanged: (v) {
                        _onTextChanged(v);
                        if (_heldModifiers.isNotEmpty) _releaseAllModifiersAfterCombo();
                      },
                      onSubmitted: (_) => _tapNamedKey('enter'),
                    ),
                  ),
                ),
                IconButton(
                  onPressed: () => _tapNamedKey('backspace'),
                  icon: const Icon(Icons.backspace_outlined),
                ),
                IconButton(
                  onPressed: () => _tapNamedKey('enter'),
                  icon: const Icon(Icons.keyboard_return),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
