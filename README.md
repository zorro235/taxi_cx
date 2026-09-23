# Taxi Telegram Mini App v4

Server-rendered Telegram Mini App for a taxi MVP: passenger/driver roles, driver approval, orders, driver offers, live GPS, profiles, trip statuses, chat, ratings, trip history, cash/Telegram payments, driver commission and admin dashboard.

## Architecture

- One Render Web Service.
- FastAPI + Uvicorn.
- SQLite database stored at `/app/storage/taxi.db`.
- Uploaded profile media stored at `/app/storage/media`.
- No separate PostgreSQL service is required for this single-service build.
- The application does not expose FastAPI/OpenAPI documentation in production.

## Required Render environment variables

```text
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=...
PUBLIC_BASE_URL=https://your-service.onrender.com
```

Optional payment variables:

```text
TELEGRAM_PAYMENT_PROVIDER_TOKEN=...
TBANK_TERMINAL_KEY=...
TBANK_PASSWORD=...
TBANK_API_URL=https://securepay.tinkoff.ru/v2
COMMISSION_RATE=0.10
PAYMENT_CURRENCY=RUB
INIT_DATA_MAX_AGE=3600
```

The Telegram bot token is never stored in the repository.

## Render settings

Use a **Web Service** with the repository root as the root directory. Do not set the root directory to `frontend`.

The root `Dockerfile` starts:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

The application serves the frontend and `/api/*` endpoints from the same service.

## Health check

```text
/health
/api/health
```

Expected response:

```json
{"ok":true,"version":"4.1.0"}
```

## Persistence

SQLite is used deliberately so the application can start with only one Render Web Service. Render's ordinary filesystem is ephemeral. This version is prepared for one Render Persistent Disk mounted at `/app/storage`; both SQLite and uploaded media live under that mount. See `RENDER_DISK_SETUP_RU.md`. A managed database is the next step before multi-instance scaling.

## Telegram authentication

Every application API call that handles user data requires Telegram `initData`. The server verifies Telegram's HMAC signature and `auth_date`; the client cannot manufacture an authenticated user by changing browser JavaScript.

## Payments

Cash works without a payment-provider credential. Online passenger payment requires a Telegram Bot Payments provider token. Driver commission SBP requires a configured T-Bank internet-acquiring terminal and is intentionally disabled until those credentials exist.
