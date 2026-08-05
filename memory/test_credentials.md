# Test Credentials

## Telegram
- Bot: @anoncoinsniper_bot (verified live via getMe)
- Admin Telegram user ID (full control): 6284967019
- Non-admin users: read-only commands only

## Secrets (stored in /app/backend/.env, never printed in logs/chat)
- TELEGRAM_BOT_TOKEN: set (see backend/.env)
- ANONCOIN_API_KEY: set (see backend/.env) - also settable via /connect (encrypted at rest)
- SOLSCAN_API_KEY: set (see backend/.env) - authenticates but plan lacks token/meta & token/holders access (401 upgrade required)
- SECRET_ENCRYPTION_KEY: generated Fernet key, set in backend/.env

## Database
- SQLite file at /app/backend/data/bot.db (auto-created on startup)

## No web app login exists - this project is Telegram-controlled, with a
## read-only status dashboard (no auth) at the frontend URL.
