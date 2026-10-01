"""
RemoteBridge admin console - database compatibility layer.

store.py was written against sqlite3. To run on Neon (serverless Postgres)
without rewriting ~60 queries, `connect()` returns a small wrapper that
exposes the same surface store.py already uses (execute / executemany /
executescript / commit / rollback / close, `cur.lastrowid`, rows readable by
name *and* by index, and an IntegrityError to catch) for either backend:

  * a filesystem path            -> sqlite3   (local dev, the test-suite)
  * postgres:// or postgresql:// -> psycopg 3 (Neon, or any Postgres)

The Postgres wrapper translates SQLite-style SQL on the fly:
  - `?` placeholders become `%s` (and literal `%` becomes `%%`)
  - `INSERT` into a table with an `id` serial column gets `RETURNING id`, which
    is what makes `cur.lastrowid` work
  - `PRAGMA` statements are ignored

Neon notes: per-request connections are short-lived (Neon wakes from
scale-to-zero on connect, so there is one retry), and server-side prepared
statements are disabled (`prepare_threshold=None`) so the pooled `-pooler`
endpoint (PgBouncer, transaction mode) works.
"""

import re
import sqlite3
import time

try:  # psycopg is only required when a Postgres URL is actually used
    import psycopg
    from psycopg import errors as _pg_errors
    _PG_INTEGRITY = (_pg_errors.IntegrityError,)
except ImportError:  # pragma: no cover - exercised only where psycopg is absent
    psycopg = None
    _PG_INTEGRITY = ()

IntegrityError = (sqlite3.IntegrityError,) + _PG_INTEGRITY

# Tables whose primary key is a serial `id` (so INSERT can RETURNING it).
_ID_TABLES = {"admin_users", "groups", "devices", "session_events", "api_keys", "feedback"}
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def is_postgres_url(target: str) -> bool:
    return isinstance(target, str) and target.lower().startswith(("postgres://", "postgresql://"))


def dialect(conn) -> str:
    return "postgres" if isinstance(conn, PgConn) else "sqlite"


class Row(dict):
    """Dict row that also supports row[0]-style positional access."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []

    def make(values):
        return Row(zip(names, values))
    return make


def _translate(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


class _Cursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


class PgConn:
    dialect = "postgres"

    def __init__(self, url: str, retries: int = 2):
        if psycopg is None:
            raise RuntimeError("DATABASE_URL points at Postgres but psycopg is not installed "
                               "(pip install 'psycopg[binary]')")
        last = None
        for attempt in range(retries + 1):
            try:
                self._conn = psycopg.connect(url, connect_timeout=15, row_factory=_row_factory,
                                             prepare_threshold=None)
                return
            except psycopg.OperationalError as e:   # Neon may still be waking up
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise last

    def execute(self, sql: str, params=()):
        if sql.lstrip().upper().startswith("PRAGMA"):
            return _Cursor(self._conn.cursor())
        m = _INSERT_RE.match(sql)
        returning = bool(m and m.group(1).lower() in _ID_TABLES and "RETURNING" not in sql.upper())
        if returning:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        try:
            cur = self._conn.execute(_translate(sql), tuple(params))
        except IntegrityError:
            self._conn.rollback()   # an aborted Postgres transaction rejects everything after it
            raise
        lastrowid = None
        if returning:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return _Cursor(cur, lastrowid)

    def executemany(self, sql: str, seq):
        cur = self._conn.cursor()
        cur.executemany(_translate(sql), [tuple(p) for p in seq])
        return _Cursor(cur)

    def executescript(self, script: str):
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                self._conn.execute(stmt)
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def connect(target: str):
    """Returns a sqlite3.Connection (Row factory set) or a PgConn."""
    if is_postgres_url(target):
        return PgConn(target)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
