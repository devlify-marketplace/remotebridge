"""WSGI entry point for hosted deploys (gunicorn wsgi:app) - used by render.yaml.

Runs the same first-run setup as `python server.py`, but non-interactively and from environment
variables (see server.bootstrap): DATABASE_URL, SECRET_KEY, ORG_ENROLLMENT_KEY, ADMIN_USERNAME,
ADMIN_PASSWORD, SESSION_EVENT_RETENTION_DAYS.
"""
import os

import server
import store

server._DB_PATH = store.DB_PATH
_conn = store.init_db(server._DB_PATH)
server.bootstrap(_conn)
store.purge_old_events(_conn, int(os.environ.get("SESSION_EVENT_RETENTION_DAYS", "90")))
_conn.close()

app = server.app
