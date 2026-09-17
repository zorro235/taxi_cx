# Taxi Telegram Mini App v3

Полноценный production-oriented MVP: пассажир, водитель, разработчик, live GPS, профили, история маршрутов, рейтинг, чат, 10% недельная комиссия, Telegram payment invoice для физической услуги.

## Важно
Оплата подключается через Telegram Bot Payments для физических услуг и требует provider token от выбранного платёжного провайдера. Без этого приложение работает, но online payment endpoint возвращает 503; наличные доступны.

## Запуск
1. Скопировать `.env.example` в `.env` и заполнить секреты.
2. `docker compose up --build`
3. HTTPS нужен для Telegram Mini App в production.
4. В BotFather назначить Main Mini App на HTTPS URL.
5. `ADMIN_TELEGRAM_IDS` — Telegram ID разработчика. Это вход без пароля через проверенную Telegram identity.
6. Для оплаты заполнить `TELEGRAM_PAYMENT_PROVIDER_TOKEN`.

## Developer
Открой Mini App с `?developer=1`. Сервер всё равно проверяет Telegram ID в `ADMIN_TELEGRAM_IDS`; параметр сам по себе доступа не даёт.

## Uber-подобная логика
Заказ → предложения водителей → выбор → ETA/GPS → статусы → поездка → оплата → рейтинг. Реализованы принципы, а не копирование кода/закрытой реализации Uber.


## T-Bank / SBP комиссия водителей

Водитель видит задолженность, нажимает «Оплатить через СБП», выбирает банк из списка Т-Банка, после чего получает индивидуальный SBP deeplink. Сервер хранит `commission_payments` и принимает уведомления Т-Банка; после подтвержденной оплаты долг уменьшается, а блокировка снимается автоматически.

Для production нужны `TBANK_TERMINAL_KEY` и `TBANK_PASSWORD`, выданные для терминала интернет-эквайринга Т-Банка, а `PUBLIC_BASE_URL` должен быть публичным HTTPS-адресом. Уведомление Т-Банка должно приходить на `/api/tbank/notification`.


## Single-service Render mode
This build does not require a separate Render PostgreSQL service. If `DATABASE_URL` is absent, the backend automatically uses SQLite at `/app/data/taxi.db` and creates the database on first start. On Render, the default filesystem is ephemeral; for persistent production data, attach a persistent disk to `/app/data` or later configure an external/managed database.
