import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token-for-unit-tests-only")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "1")
os.environ.setdefault("SECRET_ENCRYPTION_KEY", "7P55jBRgHdoIGbYXia68Lg1OytFe1ievO0OGAYXhbtk=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/test_bot.db")

