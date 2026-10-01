"""Runs the admin-console test suites against a real Postgres instead of per-test SQLite files.

    PG_TEST_URL=postgresql://user:pass@host/scratchdb python3 tests/run_admin_on_postgres.py

Every test starts from an empty `public` schema, so POINT THIS ONLY AT A SCRATCH DATABASE (e.g. a
throwaway Neon branch) - it drops everything. Works by redirecting store.get_conn to the URL and
wiping the schema whenever a test calls init_db."""
import os, sys, unittest

URL = os.environ.get("PG_TEST_URL")
if not URL:
    sys.exit("set PG_TEST_URL to a SCRATCH postgres database (it will be wiped between tests)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dbcompat, store, server

_real_connect, _real_init = dbcompat.connect, store.init_db
store.get_conn = lambda path=None: _real_connect(URL)


def wiped_init(path=None):
    try:
        c = _real_connect(URL)
        # Terminate active sessions on the scratch DB so DROP SCHEMA public CASCADE can proceed
        try:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = current_database() AND pid <> pg_backend_pid()")
            c.commit()
        except Exception:
            pass
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        c.close()
    except Exception as e:
        sys.stderr.write(f"Schema wipe warning: {e}\n")
    return _real_init(URL)



store.init_db = wiped_init
assert dbcompat.dialect(store.get_conn()) == "postgres"

suite = unittest.TestSuite()
for mod in sys.argv[1:] or ["test_admin_p12", "test_admin_2fa"]:
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName(mod))
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
