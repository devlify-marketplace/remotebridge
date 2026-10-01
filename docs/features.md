# AnyDesk-Style Remote Desktop — Feature Specification

## 1. Core Remote Access
- Unique Client ID (alias) for each device, used to connect without needing to know IP addresses
- Unattended access (connect to a device with no one present, using a saved password)
- On-demand access (session confirmed by the person at the remote device)
- Cross-platform support: Windows, macOS, Linux, iOS, Android, ChromeOS
- Session recording (record a remote session to a video file for audit/training)
- Multi-monitor support with the ability to switch between remote displays
- Session reconnect on network drop, resuming without restarting the session

## 2. Performance & Connectivity
- Low-latency screen transmission via an efficient video codec
- Automatic adjustment of image quality/frame rate based on bandwidth
- Direct (peer-to-peer) connection when possible, relay servers as fallback
- Works across NAT/firewalls without manual port forwarding
- LAN discovery for connecting to devices on the same local network

## 3. Security
- End-to-end encryption (TLS 1.2, RSA 2048 key exchange)
- Two-factor authentication (2FA) for account and device login
- Whitelisting: restrict incoming connections to an approved list of IDs
- Access control lists / permission profiles per user or device
- Session logging and audit trail
- Privacy mode (blank the remote screen so bystanders can't see activity)
- Automatic session lock/screen lock on disconnect

## 4. File & Data Transfer
- Drag-and-drop file transfer between local and remote machines
- Dedicated file manager view (two-pane, like an FTP client)
- Clipboard sync (copy/paste text and images between devices)
- Transfer resume for large or interrupted file transfers

## 5. Collaboration Tools
- Remote printing (print a document on the local printer from the remote session)
- Voice and text chat built into the session
- Whiteboard/annotation overlay for pointing things out on screen
- Multi-user sessions (several people viewing/controlling the same remote device)
- Screen-sharing-only mode (view without control)

## 6. Administration
- Central admin console for managing users, devices, and permissions across an organization
- Group policies for enforcing settings across many machines
- Custom client branding (white-label for businesses)
- Deployment via MSI/scripted install for IT rollouts
- Usage reporting and session history per user/device

## 7. Automation & Integration
- REST API for triggering connections or pulling session data
- Command-line interface for scripting unattended tasks
- Wake-on-LAN support to power on a remote machine before connecting
- Auto-update mechanism for client software

---

## 8. Mobile-Specific Features (Running the App on a Phone)

### Mobile as the *Controller* (control a PC from your phone)
- Touch-optimized remote control: tap-to-click, two-finger scroll, pinch-to-zoom
- On-screen virtual trackpad mode (drag mode) vs. direct-touch mode toggle
- On-screen keyboard with a media-key/function-key row for remote OS shortcuts (Ctrl, Alt, Esc, F1–F12)
- Gesture shortcuts (e.g., three-finger tap for right-click, swipe for Alt+Tab)
- Auto-rotate / orientation lock to match remote screen orientation
- Adaptive quality mode tuned for cellular data (lower resolution/frame rate to save bandwidth)
- Session picker optimized for small screens (favorites list, recent connections, search)
- Biometric unlock (Face ID / fingerprint) before opening the app or starting unattended access
- Background reconnect: resume a dropped session when the app returns to the foreground
- Push notifications for incoming connection requests or session events

### Mobile as the *Host* (let others remote into your phone)
- Screen mirroring/casting of the phone's display to a remote viewer
- Remote file browser scoped to the phone's accessible storage
- Permission prompts respecting mobile OS sandboxing (screen recording permission, accessibility service on Android)
- Battery-aware throttling (reduce frame rate when battery is low)

### Mobile UX & Platform Considerations
- Native iOS and Android apps (not just a browser wrapper) for performance and OS-level permissions
- Support for external keyboard/mouse/trackpad connected to the phone (tablet/DeX-style use)
- Split-screen / multi-window support on tablets
- Offline device list caching (view saved devices without a live connection)
- Low-data mode toggle for metered mobile connections
- Wi-Fi/cellular handoff without dropping the session

---

## Notes
This is a draft feature list for planning purposes — prioritize which sections (core, security, collaboration, mobile) matter most for your use case, and I can turn any section into detailed user stories or a build roadmap.


## Phase 12 - Scale & polish
- Relay: hardened, `PING`/`STATS`, load tester; client-side multi-relay failover (`--relay a,b`).
- Accessibility: admin-console landmarks/labels/captions/contrast + high-contrast theme; viewer `--speak`, `--high-contrast`.
- Localization: Spanish (unreviewed); `--lang` / `REMOTEBRIDGE_LANG` / browser language.
- Support: console Feedback inbox, `/feedback` + `/replies` in the viewer, `feedback_cli.py`.
