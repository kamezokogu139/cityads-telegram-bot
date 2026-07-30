# CityAds Telegram Bot

Telegram-бот для вебмастеров CityAds

## Установка

```bash
cd cityads-telegram-bot
python -m venv venv
source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

## Настройка

1. Создайте бота через [@BotFather](https://t.me/BotFather) и скопируйте токен.

2. Скопируйте `.env.example` → `.env` и заполните:

```bash
cp .env.example .env
```

```env
BOT_TOKEN=123456:ABC-DEF...
ENCRYPTION_KEY=
SEEDANCE_API_KEY=sk_live_...
```

3. При первом запуске без `ENCRYPTION_KEY` бот сгенерирует ключ и выведет его в консоль — скопируйте его в `.env`. Этот ключ шифрует API-ключи вебмастеров в БД.

4. Для генерации видео добавьте API-ключ Seedance 2.0 из https://seevio.ai

5. Для режимов **Image to Video** и **Reference to Video** настройте S3-совместимое хранилище (Cloudflare R2, AWS S3 и т.д.) — Seedance принимает только публичные URL медиафайлов:

```env
S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com
S3_BUCKET=my-bucket
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_PUBLIC_BASE_URL=https://pub-xxx.r2.dev
```

Режим **Text to Video** работает без S3.

## Запуск

```bash
python bot.py
```

## Для вебмастеров

1. Откройте бота в Telegram.
2. Отправьте `/connect`.
3. Введите `client_id` и `client_secret` из https://cityads.com/publisher/api
4. Готово — бот зашифрует ключи, удалит ваши сообщения из чата и будет автоматически обновлять access_token.

## Структура

```
├── bot.py            — Telegram-бот (aiogram 3)
├── cityads_api.py    — Работа с CityAds API + авто-обновление токена
├── seedance_api.py   — Генерация видео через Seedance 2.0 API
├── seedance_models.py — Режимы и модели Seedance
├── media_upload.py   — Загрузка медиа в S3 для публичных URL
├── db.py             — SQLite: хранение зашифрованных ключей
├── config.py         — Конфигурация
├── requirements.txt  — Зависимости
├── .env.example      — Шаблон переменных окружения
└── cityads.db        — БД (создаётся автоматически)
```
