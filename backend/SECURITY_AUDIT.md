# Security audit v3

- Telegram initData HMAC verification and auth_date expiry.
- Developer access is server-side Telegram-ID allowlist; `?developer=1` is only a UI switch.
- Driver must be APPROVED, online and unblocked to receive/offer trips.
- Price >= 100 is enforced by Pydantic and DB CHECK.
- Order/offer acceptance uses row locks.
- Participant checks protect trip chat and driver location.
- Driver GPS older than 2 minutes is rejected as stale.
- Uploaded profile images are MIME allowlisted and limited to 3 MB, stored under generated UUID filenames.
- User text is HTML-escaped in frontend.
- Payment completion is trusted only after Telegram sends successful_payment to the webhook; pre-checkout alone never marks paid.
- Payment payload is unique and tied to one order.
- CORS has no credentials.

Production requirements: HTTPS, strong DB password, reverse-proxy rate limiting, backups, monitoring, payment-provider credentials, Telegram webhook configuration, and local legal/driver verification requirements.
