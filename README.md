# PingSubs

Telegram bot for managing shared subscriptions: members, payment reminders, and per-person payment tracking.

## Features

- Manage subscriptions with amounts, currencies, periods, and reminder schedules
- Split costs across members (including weighted shares)
- Personal reminders with "Paid" confirmation
- Admin and member views with payment reports

## Requirements

- Python 3.11+
- Telegram bot token

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` (see `.env.example`).

### Environment variables

Required:
- `BOT_TOKEN` - Telegram bot token

Optional:
- `DATABASE_PATH` - path to SQLite database (default: `data/bot.db`)
- `REMINDER_CHECK_INTERVAL` - reminder check interval in seconds (default: `3600`)
- `TARGET_CURRENCY` - base currency for conversions (default: `RUB`)
- `CURRENCY_ROUNDING` - rounding mode for conversions: `precise`, `floor`, `round`, `ceil` (default: `precise`)

## Run

```bash
python main.py
```

## Docker

Build and run with Docker:

```bash
docker build -t pingsubs .
docker run --env-file .env -v $(pwd)/data:/app/data pingsubs
```

Or with Docker Compose:

```bash
docker compose up -d --build
```

## Usage

- Admins manage subscriptions and members via the admin menu
- Members can view their subscriptions and mark payments as completed
- Reminders are sent automatically based on the configured schedule

## Project structure

- `main.py` - entry point
- `bot/handlers/` - Telegram handlers and routing
- `bot/services/` - reminder workflow and integrations
- `bot/storage/` - database access layer (SQLite)
- `bot/core/` - config, constants, and scheduling utilities
- `bot/helpers.py` - shared UI/report helpers

## License

TBD
