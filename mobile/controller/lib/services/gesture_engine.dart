import 'dart:ui';

import '../net/session_client.dart';

/// Phase 5 - "Touch input translation: tap-to-click, drag mode vs.
/// direct-touch mode" + "Gesture shortcuts (two-finger scroll,
/// three-finger right-click, pinch-to-zoom)".
///
/// Takes raw `Listener` pointer events from the video widget (see
/// `remote_control_screen.dart`) and turns finger count + movement into
/// the mouse/scroll calls `SessionClient` already knows how to send.
/// Kept independent of Flutter's `GestureDetector` because none of the
/// built-in recognizers give per-pointer-count callbacks with the exact
/// "1 finger = cursor, 2 = scroll+pinch, 3 = right-click" mapping this
/// needs at once.
class GestureEngine {
  final SessionClient session;
  InputMode mode;

  /// Size (in logical pixels) of the widget the video is displayed in -
  /// needed to turn a touch position into the normalized [0,1] coords
  /// the wire protocol uses. Update this on every layout change.
  Size viewportSize;

  /// A local zoom applied to the displayed frame by the pinch gesture.
  /// Purely a client-side view convenience (like AnyDesk's mobile
  /// pinch-zoom) - it never changes what's sent to the host, since the
  /// host doesn't have a "zoom" concept, only its own screen resolution.
  double zoom = 1.0;
  Offset panOffset = Offset.zero;
  void Function()? onViewTransformChanged;

  GestureEngine({required this.session, required this.viewportSize, this.mode = InputMode.directTouch});

  final Map<int, Offset> _active = {};
  Offset? _lastAveragePosition;
  double? _lastPairDistance;
  Offset _trackpadCursor = const Offset(0.5, 0.5); // normalized, trackpad mode only
  DateTime? _downAt;
  bool _movedPastTapThreshold = false;
  static const _tapSlop = 8.0; // logical px

  Offset _toNormalized(Offset local) {
    final dx = (local.dx / viewportSize.width).clamp(0.0, 1.0);
    final dy = (local.dy / viewportSize.height).clamp(0.0, 1.0);
    return Offset(dx, dy);
  }

  void onPointerDown(int pointer, Offset localPosition) {
    _active[pointer] = localPosition;
    _downAt ??= DateTime.now();
    _movedPastTapThreshold = false;
    _lastAveragePosition = _average();
    _lastPairDistance = _pairDistance();

    if (_active.length == 1 && mode == InputMode.directTouch) {
      final n = _toNormalized(localPosition);
      session.sendMouseMove(n.dx, n.dy);
    }
  }

  void onPointerMove(int pointer, Offset localPosition, Offset delta) {
    if (!_active.containsKey(pointer)) return;
    _active[pointer] = localPosition;

    if (delta.distance > _tapSlop) _movedPastTapThreshold = true;

    switch (_active.length) {
      case 1:
        _handleSingleFingerMove(delta, localPosition);
        break;
      case 2:
        _handleTwoFingerMove();
        break;
      case 3:
        // Three-finger drags are reserved for the right-click tap
        // gesture (below); ignore movement so an unsteady three-finger
        // tap doesn't accidentally scroll or zoom.
        break;
    }
  }

  void onPointerUp(int pointer, Offset localPosition) {
    final wasSingleTap = _active.length == 1 && !_movedPastTapThreshold;
    final wasThreeFingerTap = _active.length == 3 && !_movedPastTapThreshold;

    if (mode == InputMode.directTouch && wasSingleTap) {
      final n = _toNormalized(localPosition);
      session.sendMouseClick(n.dx, n.dy, 'left', true);
      session.sendMouseClick(n.dx, n.dy, 'left', false);
    } else if (mode == InputMode.trackpad && wasSingleTap) {
      session.sendMouseClick(_trackpadCursor.dx, _trackpadCursor.dy, 'left', true);
      session.sendMouseClick(_trackpadCursor.dx, _trackpadCursor.dy, 'left', false);
    } else if (wasThreeFingerTap) {
      final cursor = mode == InputMode.directTouch ? _toNormalized(localPosition) : _trackpadCursor;
      session.sendMouseClick(cursor.dx, cursor.dy, 'right', true);
      session.sendMouseClick(cursor.dx, cursor.dy, 'right', false);
    }

    _active.remove(pointer);
    if (_active.isEmpty) {
      _downAt = null;
      _lastAveragePosition = null;
      _lastPairDistance = null;
    } else {
      _lastAveragePosition = _average();
      _lastPairDistance = _pairDistance();
    }
  }

  void onPointerCancel(int pointer) {
    _active.remove(pointer);
  }

  void _handleSingleFingerMove(Offset delta, Offset localPosition) {
    if (mode == InputMode.directTouch) {
      final n = _toNormalized(localPosition);
      session.sendMouseMove(n.dx, n.dy);
    } else {
      // Trackpad mode: relative movement, scaled down so a full swipe
      // across the phone doesn't fling the cursor across the whole
      // remote screen - matches the "drag mode" phrasing in the roadmap
      // (a virtual trackpad, not 1:1 finger-to-screen mapping).
      const sensitivity = 0.0022;
      _trackpadCursor = Offset(
        (_trackpadCursor.dx + delta.dx * sensitivity).clamp(0.0, 1.0),
        (_trackpadCursor.dy + delta.dy * sensitivity).clamp(0.0, 1.0),
      );
      session.sendMouseMove(_trackpadCursor.dx, _trackpadCursor.dy);
    }
  }

  void _handleTwoFingerMove() {
    final avg = _average();
    final dist = _pairDistance();
    if (avg == null) return;

    if (_lastAveragePosition != null) {
      final panDelta = avg - _lastAveragePosition!;
      // Scroll: send the pan as a scroll delta. Natural (content-follows-
      // finger) direction, matching typical touch-scroll convention.
      if (panDelta.distance > 0.5) {
        session.sendMouseScroll(panDelta.dx * 0.05, panDelta.dy * 0.05);
      }
    }

    if (_lastPairDistance != null && dist != null && _lastPairDistance! > 0) {
      final scaleDelta = dist / _lastPairDistance!;
      zoom = (zoom * scaleDelta).clamp(1.0, 4.0);
      onViewTransformChanged?.call();
    }

    _lastAveragePosition = avg;
    _lastPairDistance = dist;
  }

  Offset? _average() {
    if (_active.isEmpty) return null;
    var sum = Offset.zero;
    for (final p in _active.values) {
      sum += p;
    }
    return sum / _active.length.toDouble();
  }

  double? _pairDistance() {
    if (_active.length < 2) return null;
    final points = _active.values.toList();
    return (points[0] - points[1]).distance;
  }

  void resetZoom() {
    zoom = 1.0;
    panOffset = Offset.zero;
    onViewTransformChanged?.call();
  }
}
