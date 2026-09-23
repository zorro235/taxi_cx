# Security audit — Taxi CX 5.0

## Checked
- Telegram Mini App `initData`: HMAC verification, 10 KB size cap, expiry/future-date checks, user JSON validation.
- Admin authorization: Telegram-ID allow-list; admin actions are written to `admin_actions`.
- Driver authorization: only APPROVED + unblocked + online drivers can accept orders, send GPS, or toggle line status.
- Manual block/unblock: explicit boolean action. Unblock clears `commission_balance` to zero and marks pending/created commission payments as `WAIVED`.
- Race condition on offer acceptance: order status is checked atomically; inactive offers return 409.
- Order participant checks for order details, chat and driver location.
- Uploads: JPEG/PNG/WebP MIME and file-signature checks, 3 MB cap, UUID object names.
- Supabase Secret Key is server-only and never put into frontend code.
- FastAPI docs/OpenAPI are disabled.
- Security headers are set by middleware.
- Payment webhooks verify provider signatures and reject waived commission payments.
- SQLite remains only as a local-development fallback; Render production must use PostgreSQL + Supabase Storage.

## Tests executed
- 20 test scenarios.
- Entire 20-scenario suite repeated 20 times: **400 scenario executions, all passed**.
- Python compilation passed.
- Frontend JavaScript syntax check passed.
- Static security checks passed.

## Limits of verification
- This environment has no outbound DNS/network and no local PostgreSQL server/psycopg wheel, so a real connection to a user's Supabase project and real Telegram/T-Bank requests could not be executed here.
- Therefore this audit does not claim a live Supabase deployment was verified. The Render deployment must be tested after the user's Supabase credentials are added.
