"""
Phase 9 - Admin console storage layer.

A single SQLite file holds everything the console needs: operator
accounts (admin_users), policy bundles (groups), enrolled hosts
(devices), and the session history reported by those hosts
(session_events). No ORM - this is a small enough schema that plain
sqlite3 (or Postgres, via dbcompat.py) with Row objects (dict-like access) is easier to read and
audit than an abstraction layer would be, matching the rest of the
project's preference for the simplest thing that works.

Every host is a member of exactly one group, and a group's columns
*are* the effective policy for every device in it (see policy.py for
how a row becomes the JSON dict a host actually enforces). A "Default"
group is created on first init with everything permissive, so a freshly
enrolled device behaves exactly like Phase 8 did until an admin
deliberately locks something down.
"""

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone

import dbcompat

# A Postgres URL (Neon) in DATABASE_URL wins; otherwise a local SQLite file.
DB_PATH = os.environ.get("DATABASE_URL") or "admin.db"
IntegrityError = dbcompat.IntegrityError

POLICY_COLUMNS = [
    "allow_unattended_access",
    "allow_file_transfer",
    "allow_clipboard",
    "allow_chat",
    "allow_whiteboard",
    "allow_printing",
    "allow_voice",
    "require_view_only",
    "max_viewers",
    "max_session_minutes",
    "require_whitelist",
    "auto_update",
]

_BOOL_COLUMNS = {
    "allow_unattended_access", "allow_file_transfer", "allow_clipboard",
    "allow_chat", "allow_whiteboard", "allow_printing", "allow_voice",
    "require_view_only", "require_whitelist", "auto_update",
}

DEFAULT_GROUP_NAME = "Default"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Connection / schema --------------------------------------------------

def get_conn(db_path: str = DB_PATH):
    """SQLite file path or postgres:// URL - see dbcompat.py."""
    return dbcompat.connect(db_path)


_SCHEMA = """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_users (
            id {PK},
            username TEXT UNIQUE NOT NULL,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_throttle (
            key TEXT PRIMARY KEY,
            failures INTEGER NOT NULL DEFAULT 0,
            locked_until BIGINT NOT NULL DEFAULT 0,
            last_failure BIGINT NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS groups (
            id {PK},
            name TEXT UNIQUE NOT NULL,
            allow_unattended_access INTEGER NOT NULL DEFAULT 1,
            allow_file_transfer   INTEGER NOT NULL DEFAULT 1,
            allow_clipboard       INTEGER NOT NULL DEFAULT 1,
            allow_chat            INTEGER NOT NULL DEFAULT 1,
            allow_whiteboard      INTEGER NOT NULL DEFAULT 1,
            allow_printing        INTEGER NOT NULL DEFAULT 1,
            allow_voice           INTEGER NOT NULL DEFAULT 1,
            require_view_only    INTEGER NOT NULL DEFAULT 0,
            max_viewers           INTEGER NOT NULL DEFAULT 10,
            max_session_minutes  INTEGER NOT NULL DEFAULT 0,
            require_whitelist    INTEGER NOT NULL DEFAULT 0,
            auto_update           INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS group_allowed_viewers (
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            viewer_id TEXT NOT NULL,
            PRIMARY KEY (group_id, viewer_id)
        );

        CREATE TABLE IF NOT EXISTS group_blocked_viewers (
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            viewer_id TEXT NOT NULL,
            PRIMARY KEY (group_id, viewer_id)
        );

        CREATE TABLE IF NOT EXISTS devices (
            id {PK},
            device_id TEXT UNIQUE NOT NULL,
            display_name TEXT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            report_token TEXT UNIQUE NOT NULL,
            enrolled_at TEXT NOT NULL,
            last_seen_at TEXT,
            current_version TEXT,
            mac_address TEXT,
            last_seen_address TEXT
        );

        CREATE TABLE IF NOT EXISTS session_events (
            id {PK},
            device_id TEXT NOT NULL,
            viewer_id TEXT,
            event TEXT NOT NULL,
            decision TEXT,
            reason TEXT,
            address TEXT,
            duration_seconds DOUBLE PRECISION,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_device ON session_events(device_id);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON session_events(timestamp);

        CREATE TABLE IF NOT EXISTS api_keys (
            id {PK},
            key_hash TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id {PK},
            device_id TEXT NOT NULL,
            viewer_id TEXT,
            category TEXT NOT NULL DEFAULT 'other',
            message TEXT NOT NULL,
            client_version TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            reply TEXT,
            replied_by TEXT,
            replied_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_feedback_device ON feedback(device_id, created_at);

        CREATE TABLE IF NOT EXISTS pending_connections (
            device_id TEXT NOT NULL,
            viewer_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (device_id, viewer_id)
        );
    """


def _pk(conn) -> str:
    return "SERIAL PRIMARY KEY" if dbcompat.dialect(conn) == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"


def init_db(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA.replace("{PK}", _pk(conn)))
    conn.commit()

    # Phase 10 and 11 each added columns to tables that already existed under
    # earlier phases; ALTER TABLE upgrades a database created before the
    # column existed (CREATE TABLE IF NOT EXISTS above only helps a
    # brand-new database).
    _ensure_column(conn, "groups", "auto_update", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "devices", "current_version", "TEXT")
    _ensure_column(conn, "devices", "mac_address", "TEXT")
    _ensure_column(conn, "devices", "last_seen_address", "TEXT")
    _ensure_column(conn, "devices", "revoked_at", "TEXT")   # relay access revoked when set
    # Console 2FA (TOTP). totp_last_step blocks replaying a code inside its validity window;
    # totp_recovery is a JSON list of SHA-256 hashes of the unused single-use recovery codes.
    _ensure_column(conn, "admin_users", "totp_secret", "TEXT")
    _ensure_column(conn, "admin_users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "admin_users", "totp_last_step", "BIGINT NOT NULL DEFAULT 0")
    _ensure_column(conn, "admin_users", "totp_recovery", "TEXT")

    if get_group_by_name(conn, DEFAULT_GROUP_NAME) is None:
        create_group(conn, DEFAULT_GROUP_NAME, {})  # {} -> all columns keep their permissive defaults

    return conn


def _ensure_column(conn, table: str, column: str, coltype_and_default: str) -> None:
    if dbcompat.dialect(conn) == "postgres":
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype_and_default}")
        conn.commit()
        return
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype_and_default}")
        conn.commit()


# --- Settings (small key/value store: secret key, org enrollment key) -----

def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("""INSERT INTO settings (key, value) VALUES (?, ?)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (key, value))
    conn.commit()


# --- Password hashing (PBKDF2-HMAC-SHA256 - same recipe as desktop/auth.py,
# duplicated rather than imported since the two components run as separate
# processes with no shared package to import from) ------------------------

def _hash_password(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def _verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    if not password or not salt_b64 or not hash_b64:
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(hash_b64)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return secrets.compare_digest(digest, expected)


# --- Admin users (console operators, not remote-session viewers) ---------

def count_admin_users(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM admin_users").fetchone()["n"]


def create_admin_user(conn, username: str, password: str,
                       role: str = "admin") -> int:
    if role not in ("admin", "auditor"):
        raise ValueError("role must be 'admin' or 'auditor'")
    salt_b64, hash_b64 = _hash_password(password)
    try:
        cur = conn.execute(
            """INSERT INTO admin_users (username, password_salt, password_hash, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, salt_b64, hash_b64, role, _now_iso()),
        )
    except IntegrityError:
        raise ValueError(f"username '{username}' is already taken")
    conn.commit()
    return cur.lastrowid


def verify_login(conn, username: str, password: str):
    row = conn.execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def list_admin_users(conn) -> list:
    rows = conn.execute("SELECT id, username, role, created_at, totp_enabled FROM admin_users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def delete_admin_user(conn, user_id: int) -> None:
    if count_admin_users(conn) <= 1:
        raise ValueError("cannot delete the last remaining console operator")
    conn.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
    conn.commit()


# --- Login lockout (brute-force protection) ------------------------------
#
# Kept in the database, not in memory: the console runs under gunicorn with several workers,
# and a restart must not hand an attacker a fresh set of guesses. Two independent counters per
# attempt - one for the account name that was tried (it exists or not, so nothing leaks) and one
# for the client address - so one address can't spray many accounts and one account can't be
# guessed from many addresses without the lock kicking in. The cost: someone who knows an
# operator's username can keep that account locked (at most MAX_DELAY at a time); the address
# counter and the operator's own 2FA are what keep that from being worse than a nuisance.

FREE_ATTEMPTS = 5          # the 5th failure in a row starts the first lock (same shape as the host's throttle)
BASE_DELAY = 30            # seconds for the first lock; doubles with each further failure
MAX_DELAY = 15 * 60
FAILURE_MEMORY = 60 * 60   # failures older than this (with no lock pending) are forgotten
_KEY_MAX = 120


def throttle_keys(username: str, address: str) -> list:
    return [f"user:{(username or '').strip().lower()[:_KEY_MAX]}",
            f"ip:{(address or 'unknown')[:_KEY_MAX]}"]


def throttle_retry_after(conn, keys: list, now: float = None) -> int:
    """Seconds until every key is unlocked (0 = go ahead)."""
    now = int(now if now is not None else time.time())
    wait = 0
    for key in keys:
        row = conn.execute("SELECT locked_until FROM login_throttle WHERE key = ?", (key,)).fetchone()
        if row and row["locked_until"] > now:
            wait = max(wait, row["locked_until"] - now)
    return wait


def throttle_record_failure(conn, keys: list, now: float = None) -> None:
    now = int(now if now is not None else time.time())
    for key in keys:
        row = conn.execute("SELECT failures, locked_until, last_failure FROM login_throttle WHERE key = ?",
                           (key,)).fetchone()
        failures = 0
        if row and not (row["locked_until"] <= now and now - row["last_failure"] > FAILURE_MEMORY):
            failures = row["failures"]
        failures += 1
        over = failures - FREE_ATTEMPTS + 1      # the FREE_ATTEMPTS-th failure is the first to lock
        locked_until = now + min(MAX_DELAY, BASE_DELAY * 2 ** (over - 1)) if over > 0 else 0
        conn.execute("""INSERT INTO login_throttle (key, failures, locked_until, last_failure)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET failures = excluded.failures,
                            locked_until = excluded.locked_until, last_failure = excluded.last_failure""",
                     (key, failures, locked_until, now))
    conn.commit()


def throttle_record_success(conn, username: str) -> None:
    """A full sign-in clears that account's counter. The address counter is left to age out, so a
    script that knows one valid login can't use it to reset its guessing at other accounts."""
    conn.execute("DELETE FROM login_throttle WHERE key = ?", (throttle_keys(username, "")[0],))
    conn.commit()


def purge_throttle(conn, now: float = None) -> int:
    now = int(now if now is not None else time.time())
    cur = conn.execute("DELETE FROM login_throttle WHERE locked_until <= ? AND last_failure < ?",
                       (now, now - FAILURE_MEMORY))
    conn.commit()
    return getattr(cur, "rowcount", 0) or 0


# --- Two-factor authentication (TOTP) for operators ------------------------

RECOVERY_CODE_COUNT = 8


def _totp(secret: str):
    import pyotp
    return pyotp.TOTP(secret)


def new_totp_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def totp_uri(secret: str, username: str, issuer: str) -> str:
    return _totp(secret).provisioning_uri(name=username, issuer_name=issuer)


def _recovery_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode()).hexdigest()


def _new_recovery_codes() -> list:
    # 10 base32-ish chars from a 32-symbol alphabet = 50 bits each: far beyond guessing once
    # the lockout is in place, so a plain SHA-256 (no slow KDF) is the right storage.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["".join(secrets.choice(alphabet) for _ in range(10)) for _ in range(RECOVERY_CODE_COUNT)]


def get_admin_user(conn, user_id: int):
    row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def enable_totp(conn, user_id: int, secret: str, code: str, now: float = None):
    """Turns 2FA on if `code` is valid for `secret`. Returns the one-time recovery codes
    (formatted xxxxx-xxxxx) or None if the code was wrong."""
    now = now if now is not None else time.time()
    totp = _totp(secret)
    if not code or not totp.verify(code.strip().replace(" ", ""), for_time=now, valid_window=1):
        return None
    codes = _new_recovery_codes()
    conn.execute("""UPDATE admin_users SET totp_secret = ?, totp_enabled = 1, totp_last_step = ?,
                    totp_recovery = ? WHERE id = ?""",
                 (secret, int(now // 30), json.dumps([_recovery_hash(c) for c in codes]), user_id))
    conn.commit()
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def disable_totp(conn, user_id: int) -> None:
    conn.execute("""UPDATE admin_users SET totp_secret = NULL, totp_enabled = 0, totp_last_step = 0,
                    totp_recovery = NULL WHERE id = ?""", (user_id,))
    conn.commit()


def verify_second_factor(conn, user_id: int, code: str, now: float = None) -> str:
    """Returns "totp", "recovery", or "" (rejected). A TOTP code is accepted once: a code from the
    same or an earlier 30-second step than the last accepted one is a replay. A recovery code is
    spent the moment it is used."""
    now = now if now is not None else time.time()
    user = get_admin_user(conn, user_id)
    code = (code or "").strip()
    if not user or not user["totp_enabled"] or not code:
        return ""
    digits = code.replace(" ", "")
    if digits.isdigit() and len(digits) == 6:
        totp = _totp(user["totp_secret"])
        for offset in (-1, 0, 1):                       # same +/-1 step tolerance as the host
            step = int(now // 30) + offset
            if secrets.compare_digest(totp.at(step * 30), digits):
                if step <= user["totp_last_step"]:
                    return ""
                cur = conn.execute("""UPDATE admin_users SET totp_last_step = ?
                                      WHERE id = ? AND totp_last_step < ?""", (step, user_id, step))
                conn.commit()
                return "totp" if getattr(cur, "rowcount", 1) else ""
        return ""
    hashes = json.loads(user["totp_recovery"] or "[]")
    wanted = _recovery_hash(code)
    for h in hashes:
        if secrets.compare_digest(h, wanted):
            hashes.remove(h)
            conn.execute("UPDATE admin_users SET totp_recovery = ? WHERE id = ?", (json.dumps(hashes), user_id))
            conn.commit()
            return "recovery"
    return ""


def recovery_codes_left(conn, user_id: int) -> int:
    user = get_admin_user(conn, user_id)
    return len(json.loads(user["totp_recovery"] or "[]")) if user else 0


def change_password_check(conn, user_id: int, password: str) -> bool:
    user = get_admin_user(conn, user_id)
    return bool(user) and _verify_password(password, user["password_salt"], user["password_hash"])


# --- Groups (policy bundles) ----------------------------------------------

_POLICY_DEFAULTS = {
    "allow_unattended_access": 1, "allow_file_transfer": 1, "allow_clipboard": 1,
    "allow_chat": 1, "allow_whiteboard": 1, "allow_printing": 1, "allow_voice": 1,
    "require_view_only": 0, "max_viewers": 10, "max_session_minutes": 0,
    "require_whitelist": 0, "auto_update": 1,
}


def _normalize_policy(policy: dict) -> dict:
    """Fills in defaults for any column not supplied, coercing booleans to 0/1."""
    merged = dict(_POLICY_DEFAULTS)
    merged.update({k: v for k, v in policy.items() if k in POLICY_COLUMNS})
    for col in _BOOL_COLUMNS:
        merged[col] = 1 if merged[col] else 0
    merged["max_viewers"] = int(merged["max_viewers"])
    merged["max_session_minutes"] = int(merged["max_session_minutes"])
    return merged


def create_group(conn, name: str, policy: dict) -> int:
    p = _normalize_policy(policy)
    cols = ", ".join(POLICY_COLUMNS)
    placeholders = ", ".join("?" for _ in POLICY_COLUMNS)
    try:
        cur = conn.execute(
            f"INSERT INTO groups (name, {cols}, created_at) VALUES (?, {placeholders}, ?)",
            (name, *[p[c] for c in POLICY_COLUMNS], _now_iso()),
        )
    except IntegrityError:
        raise ValueError(f"a group named '{name}' already exists")
    conn.commit()
    return cur.lastrowid


def update_group_policy(conn, group_id: int, policy: dict) -> None:
    current = get_group(conn, group_id)
    if current is None:
        raise ValueError("no such group")
    p = _normalize_policy({**{c: current[c] for c in POLICY_COLUMNS}, **policy})
    assignments = ", ".join(f"{c} = ?" for c in POLICY_COLUMNS)
    conn.execute(f"UPDATE groups SET {assignments} WHERE id = ?",
                 (*[p[c] for c in POLICY_COLUMNS], group_id))
    conn.commit()


def rename_group(conn, group_id: int, new_name: str) -> None:
    try:
        conn.execute("UPDATE groups SET name = ? WHERE id = ?", (new_name, group_id))
    except IntegrityError:
        raise ValueError(f"a group named '{new_name}' already exists")
    conn.commit()


def get_group(conn, group_id: int):
    row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    return dict(row) if row else None


def get_group_by_name(conn, name: str):
    row = conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def list_groups(conn) -> list:
    rows = conn.execute("""
        SELECT groups.*, COUNT(devices.id) AS device_count
        FROM groups LEFT JOIN devices ON devices.group_id = groups.id
        GROUP BY groups.id
        ORDER BY (groups.name != 'Default'), groups.name
    """).fetchall()
    return [dict(r) for r in rows]


def delete_group(conn, group_id: int) -> None:
    group = get_group(conn, group_id)
    if group is None:
        return
    if group["name"] == DEFAULT_GROUP_NAME:
        raise ValueError("the Default group can't be deleted")
    default_id = get_group_by_name(conn, DEFAULT_GROUP_NAME)["id"]
    conn.execute("UPDATE devices SET group_id = ? WHERE group_id = ?", (default_id, group_id))
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()


def set_group_viewer_list(conn, group_id: int, kind: str, viewer_ids: list) -> None:
    table = {"allowed": "group_allowed_viewers", "blocked": "group_blocked_viewers"}[kind]
    conn.execute(f"DELETE FROM {table} WHERE group_id = ?", (group_id,))
    cleaned = sorted({v.strip() for v in viewer_ids if v.strip()})
    conn.executemany(f"INSERT INTO {table} (group_id, viewer_id) VALUES (?, ?)",
                      [(group_id, v) for v in cleaned])
    conn.commit()


def get_group_viewer_lists(conn, group_id: int) -> tuple:
    allowed = [r["viewer_id"] for r in conn.execute(
        "SELECT viewer_id FROM group_allowed_viewers WHERE group_id = ? ORDER BY viewer_id", (group_id,))]
    blocked = [r["viewer_id"] for r in conn.execute(
        "SELECT viewer_id FROM group_blocked_viewers WHERE group_id = ? ORDER BY viewer_id", (group_id,))]
    return allowed, blocked


# --- Devices ---------------------------------------------------------------

def enroll_device(conn, device_id: str, display_name: str = None):
    """Idempotent: re-enrolling a device_id that already exists just returns
    its existing record (and token) rather than erroring, so a host that
    lost its local enrollment file can safely re-run --admin-url setup as
    long as it still knows its own device_id."""
    existing = get_device_by_device_id(conn, device_id)
    if existing is not None:
        return existing

    default_group = get_group_by_name(conn, DEFAULT_GROUP_NAME)
    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO devices (device_id, display_name, group_id, report_token, enrolled_at)
           VALUES (?, ?, ?, ?, ?)""",
        (device_id, display_name or device_id, default_group["id"], token, _now_iso()),
    )
    conn.commit()
    return get_device_by_device_id(conn, device_id)


def get_device_by_device_id(conn, device_id: str):
    row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def get_device_by_token(conn, token: str):
    row = conn.execute("SELECT * FROM devices WHERE report_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def list_devices(conn) -> list:
    rows = conn.execute("""
        SELECT devices.*, groups.name AS group_name
        FROM devices JOIN groups ON groups.id = devices.group_id
        ORDER BY devices.last_seen_at IS NULL, devices.last_seen_at DESC
    """).fetchall()
    return [dict(r) for r in rows]


def set_device_revoked(conn, device_id: str, revoked: bool) -> bool:
    """Cut a device off from the relay (or restore it). Returns False if no such device. Takes effect on
    relays as they next poll /api/v1/relay/revoked (30 s by default) and immediately stops the console
    handing the device a relay token."""
    if get_device_by_device_id(conn, device_id) is None:
        return False
    conn.execute("UPDATE devices SET revoked_at = ? WHERE device_id = ?",
                 (_now_iso() if revoked else None, device_id))
    conn.commit()
    return True


def list_revoked_device_ids(conn) -> list:
    rows = conn.execute("SELECT device_id FROM devices WHERE revoked_at IS NOT NULL "
                        "ORDER BY device_id").fetchall()
    return [r["device_id"] for r in rows]


def assign_device_group(conn, device_id: str, group_id: int) -> None:
    conn.execute("UPDATE devices SET group_id = ? WHERE device_id = ?", (group_id, device_id))
    conn.commit()


def rename_device(conn, device_id: str, display_name: str) -> None:
    conn.execute("UPDATE devices SET display_name = ? WHERE device_id = ?", (display_name, device_id))
    conn.commit()


def touch_last_seen(conn, device_id: str, source_address: str = None) -> None:
    if source_address:
        conn.execute("UPDATE devices SET last_seen_at = ?, last_seen_address = ? WHERE device_id = ?",
                     (_now_iso(), source_address, device_id))
    else:
        conn.execute("UPDATE devices SET last_seen_at = ? WHERE device_id = ?", (_now_iso(), device_id))
    conn.commit()


def record_version(conn, device_id: str, version: str) -> None:
    """Phase 10: a host reports its own build version on every policy fetch
    (see /api/v1/policy's client_version param) so the Devices page can show
    IT which machines are behind, without a separate reporting round-trip."""
    conn.execute("UPDATE devices SET current_version = ? WHERE device_id = ?", (version, device_id))
    conn.commit()


def set_device_mac_address(conn, device_id: str, mac_address: str) -> None:
    """Phase 11: set either by a host reporting its own MAC alongside a
    policy fetch (see admin_client.get_own_mac_address), or by an admin
    typing one in on the Devices page for a host that hasn't - a MAC
    address is what Wake-on-LAN needs (see wol.py), and a self-reported
    one can be wrong (multiple NICs, a VM's virtual adapter) often enough
    that the manual override has to stay available."""
    conn.execute("UPDATE devices SET mac_address = ? WHERE device_id = ?",
                 (mac_address.strip(), device_id))
    conn.commit()


# --- Session history ---------------------------------------------------

def record_event(conn, device_id: str, event: str, viewer_id: str = None,
                  decision: str = None, reason: str = None, address: str = None,
                  duration_seconds: float = None) -> None:
    conn.execute(
        """INSERT INTO session_events
           (device_id, viewer_id, event, decision, reason, address, duration_seconds, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (device_id, viewer_id, event, decision, reason, address, duration_seconds, _now_iso()),
    )
    conn.commit()


def list_events(conn, device_id: str = None, event: str = None,
                 since_iso: str = None, limit: int = 200) -> list:
    clauses, params = [], []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if event:
        clauses.append("event = ?")
        params.append(event)
    if since_iso:
        clauses.append("timestamp >= ?")
        params.append(since_iso)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM session_events {where} ORDER BY timestamp DESC LIMIT ?", params
    ).fetchall()
    return [dict(r) for r in rows]


def purge_old_events(conn, days: int) -> int:
    """Deletes session_events older than `days` (0 = keep everything). Neon's free tier has a
    small storage cap, and the event log is the one table that grows without bound."""
    if days <= 0:
        return 0
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, tz=timezone.utc).isoformat()
    cur = conn.execute("DELETE FROM session_events WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def usage_summary(conn) -> dict:
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    week_start_ts = time.time() - 7 * 86400
    week_start = datetime.fromtimestamp(week_start_ts, tz=timezone.utc).isoformat()

    total_devices = conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
    total_groups = conn.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
    sessions_today = conn.execute(
        "SELECT COUNT(*) AS n FROM session_events WHERE event = 'start' AND timestamp >= ?",
        (today_start,)).fetchone()["n"]
    sessions_week = conn.execute(
        "SELECT COUNT(*) AS n FROM session_events WHERE event = 'start' AND timestamp >= ?",
        (week_start,)).fetchone()["n"]
    rejected_week = conn.execute(
        "SELECT COUNT(*) AS n FROM session_events WHERE event = 'attempt' "
        "AND decision NOT LIKE 'auto_%' AND decision NOT LIKE 'manual_accept' AND timestamp >= ?",
        (week_start,)).fetchone()["n"]

    top_devices = conn.execute(
        """SELECT device_id, COUNT(*) AS sessions FROM session_events
           WHERE event = 'start' AND timestamp >= ?
           GROUP BY device_id ORDER BY sessions DESC LIMIT 5""",
        (week_start,)).fetchall()

    return {
        "total_devices": total_devices,
        "total_groups": total_groups,
        "sessions_today": sessions_today,
        "sessions_week": sessions_week,
        "rejected_week": rejected_week,
        "top_devices": [dict(r) for r in top_devices],
    }


# --- Phase 10: branding + release info, both stored in the same small
# settings key/value table already used for the secret key and org
# enrollment key. Both are org-wide (not per-group, unlike policy). ------

_BRANDING_DEFAULTS = {"display_name": "RemoteBridge", "support_url": ""}


def get_branding(conn) -> dict:
    return {
        "display_name": get_setting(conn, "branding_display_name", _BRANDING_DEFAULTS["display_name"]),
        "support_url": get_setting(conn, "branding_support_url", _BRANDING_DEFAULTS["support_url"]),
    }


def set_branding(conn, display_name: str, support_url: str) -> None:
    set_setting(conn, "branding_display_name", display_name.strip() or _BRANDING_DEFAULTS["display_name"])
    set_setting(conn, "branding_support_url", support_url.strip())


def get_release(conn):
    """Returns None if no release has ever been published (the common case
    right after an install - hosts simply see no update available)."""
    version = get_setting(conn, "release_version")
    if not version:
        return None
    return {
        "version": version,
        "download_url": get_setting(conn, "release_download_url", ""),
        "sha256": get_setting(conn, "release_sha256", ""),
        "notes": get_setting(conn, "release_notes", ""),
        "published_at": get_setting(conn, "release_published_at", ""),
    }


def set_release(conn, version: str, download_url: str,
                 sha256: str, notes: str) -> None:
    set_setting(conn, "release_version", version.strip())
    set_setting(conn, "release_download_url", download_url.strip())
    set_setting(conn, "release_sha256", sha256.strip())
    set_setting(conn, "release_notes", notes.strip())
    set_setting(conn, "release_published_at", _now_iso())


# --- Phase 11: operator API keys, for scripts/other systems (cli/, or any
# direct caller of /api/v1/ops/) rather than a person's browser session.
# The raw key is only ever returned once, at creation - only its hash is
# stored, the same principle as password storage elsewhere in this
# project, even though a random high-entropy key doesn't need PBKDF2's
# deliberate slowness the way a human-chosen password does. -------------

def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(conn, label: str) -> str:
    raw_key = "dvfy_" + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO api_keys (key_hash, label, created_at) VALUES (?, ?, ?)",
                 (_hash_api_key(raw_key), label.strip() or "unlabeled", _now_iso()))
    conn.commit()
    return raw_key


def verify_api_key(conn, raw_key: str):
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?",
                        (_hash_api_key(raw_key),)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now_iso(), row["id"]))
    conn.commit()
    return dict(row)


def list_api_keys(conn) -> list:
    rows = conn.execute("SELECT id, label, created_at, last_used_at FROM api_keys "
                         "ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(conn, key_id: int) -> None:
    conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    conn.commit()


# --- Phase 11: one-time pre-authorized connections, issued by
# POST /api/v1/ops/devices/<id>/connect and consumed by a host's
# GET /api/v1/preauth/<viewer_id> at the moment that viewer_id actually
# tries to connect (see host_p11.py's perform_host_auth_with_preauth). --

def create_pending_connection(conn, device_id: str, ttl_seconds: int = 300) -> dict:
    _purge_expired_pending_connections(conn)
    viewer_id = "auto-" + secrets.token_hex(4)
    expires_at = datetime.fromtimestamp(time.time() + ttl_seconds, tz=timezone.utc).isoformat()
    conn.execute("INSERT INTO pending_connections (device_id, viewer_id, expires_at, created_at) "
                 "VALUES (?, ?, ?, ?)", (device_id, viewer_id, expires_at, _now_iso()))
    conn.commit()
    return {"device_id": device_id, "viewer_id": viewer_id, "expires_at": expires_at}


def check_and_consume_preauth(conn, device_id: str, viewer_id: str) -> bool:
    """Single-use: a matching, unexpired row is deleted the moment it's
    found, so the same one-time viewer_id can't be replayed for a second
    connection. Returns False (never raises) for "no such pending
    connection" and "it existed but already expired" alike - the caller
    (the host, over HTTP) doesn't need to distinguish those."""
    row = conn.execute(
        "SELECT expires_at FROM pending_connections WHERE device_id = ? AND viewer_id = ?",
        (device_id, viewer_id)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM pending_connections WHERE device_id = ? AND viewer_id = ?",
                 (device_id, viewer_id))
    conn.commit()
    return row["expires_at"] > _now_iso()


def _purge_expired_pending_connections(conn) -> None:
    conn.execute("DELETE FROM pending_connections WHERE expires_at <= ?", (_now_iso(),))
    conn.commit()


# --- Support / feedback channel (Phase 12) -------------------------------------
#
# Hosts (and, through their host, the viewers connected to them) can file a
# ticket with the org's admins; admins reply from the console and the sender
# can read the reply back. A ticket belongs to the device that submitted it - a
# device can only ever read its own (see server.py's GET /api/v1/feedback).

FEEDBACK_CATEGORIES = ("bug", "question", "idea", "other")
FEEDBACK_MAX_MESSAGE = 4000
FEEDBACK_MAX_REPLY = 4000
FEEDBACK_RATE_LIMIT = 20          # tickets per device...
FEEDBACK_RATE_WINDOW = 3600       # ...per this many seconds


class FeedbackError(ValueError):
    """Raised with a message safe to show the submitter."""


def create_feedback(conn, device_id: str, message: str, category: str = "other",
                    viewer_id: str = None, client_version: str = None) -> int:
    message = (message or "").strip()
    if not message:
        raise FeedbackError("message is empty")
    if len(message) > FEEDBACK_MAX_MESSAGE:
        raise FeedbackError(f"message is too long (max {FEEDBACK_MAX_MESSAGE} characters)")
    if category not in FEEDBACK_CATEGORIES:
        category = "other"
    if recent_feedback_count(conn, device_id) >= FEEDBACK_RATE_LIMIT:
        raise FeedbackError("too many feedback submissions from this device; try again later")
    cur = conn.execute(
        """INSERT INTO feedback (device_id, viewer_id, category, message, client_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (device_id, (viewer_id or "")[:100] or None, category, message,
         (client_version or "")[:40] or None, _now_iso()))
    conn.commit()
    return cur.lastrowid


def recent_feedback_count(conn, device_id: str,
                          window_seconds: int = FEEDBACK_RATE_WINDOW) -> int:
    since = datetime.fromtimestamp(time.time() - window_seconds, tz=timezone.utc).isoformat()
    return conn.execute("SELECT COUNT(*) FROM feedback WHERE device_id = ? AND created_at >= ?",
                        (device_id, since)).fetchone()[0]


def list_feedback(conn, status: str = None, device_id: str = None,
                  limit: int = 200) -> list:
    clauses, params = [], []
    if status in ("open", "resolved"):
        clauses.append("status = ?")
        params.append(status)
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(f"SELECT * FROM feedback {where} ORDER BY id DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


def get_feedback(conn, feedback_id: int):
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    return dict(row) if row else None


def count_open_feedback(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'open'").fetchone()[0]


def reply_to_feedback(conn, feedback_id: int, reply: str, replied_by: str,
                      resolve: bool = False) -> None:
    reply = (reply or "").strip()
    if not reply:
        raise FeedbackError("reply is empty")
    if len(reply) > FEEDBACK_MAX_REPLY:
        raise FeedbackError(f"reply is too long (max {FEEDBACK_MAX_REPLY} characters)")
    cur = conn.execute(
        """UPDATE feedback SET reply = ?, replied_by = ?, replied_at = ?,
                               status = CASE WHEN ? = 1 THEN 'resolved' ELSE status END
           WHERE id = ?""",
        (reply, replied_by, _now_iso(), 1 if resolve else 0, feedback_id))
    conn.commit()
    if cur.rowcount == 0:
        raise FeedbackError("no such feedback")


def set_feedback_status(conn, feedback_id: int, status: str) -> None:
    if status not in ("open", "resolved"):
        raise FeedbackError("status must be open or resolved")
    conn.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id))
    conn.commit()
