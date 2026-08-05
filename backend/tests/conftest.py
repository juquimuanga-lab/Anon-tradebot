import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token-for-unit-tests-only")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "1")
os.environ.setdefault("SECRET_ENCRYPTION_KEY", "7P55jBRgHdoIGbYXia68Lg1OytFe1ievO0OGAYXhbtk=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/test_bot.db")

# The test DB is a persistent file on disk (not in-memory) so that all tests
# in a session share the same connection/tables. init_db() only does
# create_all (never drops/clears rows), so without removing the stale file
# here, re-running pytest across sessions accumulates duplicate rows for
# tests that use fixed mint strings (e.g. test_positions_manager.py),
# causing flaky "expected 1, got N" assertion failures on reruns.
_test_db_path = os.path.join(os.path.dirname(__file__), "test_bot.db")
if os.path.exists(_test_db_path):
    os.remove(_test_db_path)

