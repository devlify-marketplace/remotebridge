# RemoteBridge

**RemoteBridge** is an enterprise-grade, cross-platform remote desktop streaming, collaboration, and fleet management system built in Python, Flutter, and HTML5. It serves as a secure, self-hostable alternative to AnyDesk and TeamViewer.

---

## 🌟 Key Features

- **Cross-Platform Desktop & Mobile**:
  - **Desktop Host & Viewer**: Low-latency video streaming, mouse/keyboard control, multi-monitor switching, adaptive quality/FPS, clipboard sync, file transfer with auto-resume, and session recording.
  - **Tkinter GUI (`desktop/gui.py`)**: User-friendly desktop graphical wrapper for non-technical users.
  - **Flutter Mobile Suite**: Mobile Controller (`mobile/controller`) to remotely control PCs from iOS/Android and Mobile Host (`mobile/host`) for phone screen sharing.
- **Direct P2P & Hardened Relay Infrastructure**:
  - **STUN / UDP NAT Traversal (`desktop/p2p.py`)**: Direct peer-to-peer connection via UDP hole punching.
  - **ID-Based TLS Relay (`server/relay.py`)**: Multi-relay failover, HMAC-authenticated registration, token expiry, IP ban throttling, and per-device revocation.
  - **Prometheus Metrics**: Scraper-friendly metric endpoints (`/metrics`) on both relay and admin nodes.
- **Centralized Admin Console & Policy Engine (`admin/`)**:
  - Web console with SQLite & PostgreSQL (16/18) backends.
  - Multi-tenant group policy enforcement (unattended access, clipboard, printing, file transfer, whiteboard, voice, viewer caps, time limits, allow/block lists).
  - 2FA/TOTP authentication, login failure lockout, operator role control (Admin vs Auditor), feedback inbox, custom branding, and auto-update release distribution.
- **Security & Access Control**:
  - Certificate pinning, exponential login throttling, out-of-band PIN verification, and encrypted TLS command streams.
- **Automation & Remote Operations (`cli/`)**:
  - `cli/remotebridge.py` for scripted headless access, session log extraction, and Wake-on-LAN (WoL) execution.
- **Automated CI/CD**:
  - GitHub Actions workflow (`.github/workflows/ci.yml`) validating Python unit/e2e tests, Postgres integration, and Flutter static code health.

---

## 📁 Repository Structure

```
remotebridge/
├── desktop/               # Python Desktop Host, Viewer, GUI & Core Protocol
│   ├── gui.py              # Cross-platform Tkinter GUI launcher
│   ├── host_p12.py         # Multi-user, policy-aware Host engine
│   ├── viewer_p12.py       # Adaptive, TLS-encrypted Viewer client
│   ├── protocol_p12.py     # Shared binary wire protocol
│   ├── p2p.py              # STUN client & UDP hole punching module
│   ├── audio_chat.py       # Live audio streaming & recording mixer
│   ├── auth.py             # Authentication, 2FA, rate-limiting & pinning
│   ├── file_transfer.py    # Resumable chunked file transfer
│   └── requirements.txt    # Desktop Python dependencies
├── server/                # Relay & Signaling Infrastructure
│   ├── relay.py            # Hardened TLS Relay server with metrics & revocation
│   ├── loadtest.py         # Concurrent stress test harness
│   └── Dockerfile          # Production container configuration
├── admin/                 # Central Management Console & Policy Server
│   ├── server.py           # Flask administration & policy API server
│   ├── store.py            # SQLite / PostgreSQL data access layer
│   ├── policy.py           # Group policy resolution engine
│   └── templates/          # Responsive admin console web interfaces
├── mobile/                # Flutter Mobile Applications
│   ├── controller/         # Mobile Remote Controller app (iOS / Android)
│   └── host/               # Mobile Host screen-sharing app
├── cli/                   # Remote Operations & Automation CLI
│   └── remotebridge.py     # Scripted API client & WoL execution tool
├── deploy/                # Deployment Scripts & Installers
│   ├── linux/install.sh    # Linux systemd service & venv installer
│   └── wix/                # Windows MSI packaging configuration
└── tests/                 # Comprehensive Automated Test Suite
    ├── test_p2p.py         # STUN and UDP hole punching tests
    ├── test_relay_tls.py   # Relay TLS socket verification
    └── run_admin_on_postgres.py # Postgres integration test harness
```

---

## 🚀 Quick Start Guide

### 1. Prerequisites
Ensure Python 3.10+ and `pip` are installed:
```bash
pip install -r desktop/requirements.txt
pip install -r admin/requirements.txt
```

### 2. Master All-in-One System Launcher
Launch all RemoteBridge modules simultaneously (Relay, Admin Console, Host, and GUI) with a single command:
```bash
python3 start_all.py
# Or run in headless mode (backend modules only):
python3 start_all.py --headless
```

### 3. Graphical Desktop App
To launch the interactive GUI on Linux, macOS, or Windows:
```bash
python3 desktop/gui.py
```


### 3. Command-Line Host & Viewer

#### **Host (Device Sharing)**:
Start a desktop host session registered with a local or public relay:
```bash
python3 desktop/host_p12.py --device-id my-pc-01 --relay relay.example.com:6000
```

#### **Viewer (Remote Control)**:
Connect to the host session by device ID:
```bash
python3 desktop/viewer_p12.py --target my-pc-01 --relay relay.example.com:6000
```

---

## 🔐 Admin Console & Policy Server

Launch the administrative portal:
```bash
python3 admin/server.py --port 8443
```
- Open `http://localhost:8443` in a web browser.
- Create an operator account, set up 2FA TOTP, enroll host devices using the generated enrollment key, configure group policies, and review session audit logs.
- Scrape operational metrics at `http://localhost:8443/metrics`.

---

## 📡 Relay Server Deployment

Run the Relay Server for WAN traversal:
```bash
python3 server/relay.py --port 6000 --secret "your-shared-relay-secret" --stats-open
```
- **Prometheus Metrics**: Query the relay for statistics via the `METRICS` socket command.

---

## 📱 Mobile Applications (Flutter)

Navigate to the respective mobile app directory to build and run using Flutter:

```bash
# Run Mobile Remote Controller
cd mobile/controller
flutter pub get
flutter run

# Run Mobile Host
cd mobile/host
flutter pub get
flutter run
```

---

## 🧪 Testing & Quality Assurance

Run the complete Python unit and e2e test suite:
```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Run administrative integration tests against PostgreSQL:
```bash
PG_TEST_URL="postgresql://postgres:password@localhost:5432/remotebridge_test" python3 tests/run_admin_on_postgres.py
```

---

## 🛠️ CI/CD Pipeline

The project uses GitHub Actions (`.github/workflows/ci.yml`) to automatically test every push and pull request against:
1. Python unit and e2e test coverage.
2. Real PostgreSQL 16 service integration.
3. Flutter static analysis (`flutter analyze`) for both controller and host apps.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
