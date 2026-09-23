# Перевод Taxi CX на Supabase + Render Free

1. В Supabase создайте Free project.
2. Создайте Storage bucket `profile-photos` и включите Public для аватаров.
3. В Supabase нажмите **Connect** → **Session pooler** → порт 5432 и скопируйте URI.
4. В Render → Environment добавьте `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_STORAGE_BUCKET=profile-photos`, `SUPABASE_STORAGE_PUBLIC=true`.
5. `SUPABASE_SECRET_KEY` не вставлять в GitHub и frontend.
6. Root Directory Render должен оставаться пустым (корень репозитория), потому что Dockerfile находится в корне.
7. После deploy откройте `/health`. В ответе должно быть `version: 5.0.0`, `database: postgresql`, `storage: supabase`.

### Карта
- Библиотека интерфейса: Leaflet 1.9.4 через CDN.
- Тайлы по умолчанию: OpenStreetMap.
- Адреса: Nominatim (reverse geocoding/geocoding).
- На карте есть обязательная подпись OpenStreetMap.
- Для небольшого MVP это оставлено как бесплатный вариант. При росте нагрузки лучше перейти на специализированного провайдера карт/тайлов, соблюдая его тариф и лимиты.

### Что хранится где
- PostgreSQL Supabase: пользователи, заказы, предложения, поездки, GPS, чат, рейтинги, платежи, комиссии, журнал действий администратора.
- Supabase Storage: фотографии профилей.
- Render: только код приложения и временные файлы.
