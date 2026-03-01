# CityAds Telegram Bot

Telegram-бот для вебмастеров CityAds — баланс, статистика, офферы, купоны прямо в мессенджере.

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
```

3. При первом запуске без `ENCRYPTION_KEY` бот сгенерирует ключ и выведет его в консоль — скопируйте его в `.env`. Этот ключ шифрует API-ключи вебмастеров в БД.

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
├── db.py             — SQLite: хранение зашифрованных ключей
├── config.py         — Конфигурация
├── requirements.txt  — Зависимости
├── .env.example      — Шаблон переменных окружения
└── cityads.db        — БД (создаётся автоматически)
```
