# Настройка бесплатного Supabase для Taxi CX

## 1. PostgreSQL
1. Создайте бесплатный проект Supabase.
2. Откройте **Connect**.
3. Выберите **Session pooler**, порт **5432**.
4. Скопируйте URI в Render как `DATABASE_URL`.
5. Для SQLAlchemy используйте префикс `postgresql+psycopg://`.

## 2. Storage
Создайте bucket:
- имя: `profile-photos`
- Public: **ON** для аватаров профилей.
- ограничение MIME: `image/jpeg,image/png,image/webp`
- размер файла: 3 MB или меньше.

Приложение загружает файлы только с сервера Render через Supabase Secret Key. Этот ключ нельзя помещать во frontend.

## 3. Переменные Render
Добавьте:
- `DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`
- `SUPABASE_STORAGE_BUCKET=profile-photos`
- `SUPABASE_STORAGE_PUBLIC=true`

Остальные Telegram/T-Bank переменные оставьте как в существующем сервисе.

## 4. Первый запуск
При старте приложение само создаёт все таблицы в PostgreSQL.
Локальная SQLite-база больше не используется, если `DATABASE_URL` задан.

## 5. Бесплатные лимиты
На бесплатном плане Supabase сейчас: 500 MB фактического размера базы и 1 GB Storage. При превышении лимита базы включается read-only режим. Это подходит для MVP/небольшой первой эксплуатации, но не следует считать бессрочной промышленной инфраструктурой.

## 6. Важное по Render Free
Render Free имеет эфемерную файловую систему, поэтому SQLite и локальные фотографии нельзя считать постоянными. После миграции постоянные данные находятся в Supabase.
