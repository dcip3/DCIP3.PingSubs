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

Create the local data directory for SQLite:

```bash
mkdir -p data
```

### Git commit message policy

The repository includes a `commit-msg` hook that allows commit messages only in English.

Enable repository hooks once:

```bash
git config core.hooksPath .githooks
```

### Environment variables

Required:
- `BOT_TOKEN` - Telegram bot token

Optional:
- `DATABASE_PATH` - path to SQLite database (default: `data/app.db`)
- `REMINDER_CHECK_INTERVAL` - reminder check interval in seconds (default: `3600`)
- `BASE_REMINDER_TIME` - default reminder time in `HH:MM` (default: `16:00`)
- `BASE_TIMEZONE` - default timezone in IANA format (default: `Europe/Moscow`)
- `TARGET_CURRENCY` - base currency for conversions (default: `RUB`)
- `CURRENCY_ROUNDING` - rounding mode for conversions: `precise`, `floor`, `round`, `ceil` (default: `precise`)
- `ADMIN_IDS` - comma-separated Telegram IDs that should be admins on startup

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
- On first launch without `ADMIN_IDS`, the first user who sends `/start` becomes admin automatically
- Members can set a personal base currency in `⚙️ Settings`; if not set, the admin base currency is used
- Both admin and members can set base reminder time in `⚙️ Settings`
- Both admin and members can set timezone in `⚙️ Settings`
- Reminder time precedence is: personal subscription override -> subscription override (admin) -> personal base time -> admin base time
- Reminder timezone precedence is: personal timezone -> admin timezone
- Reminder times are interpreted in the recipient's effective timezone


## Project structure

- `main.py` - entry point
- `app/handlers/` - Telegram handlers and routing
- `app/services/` - reminder workflow and integrations
- `app/storage/` - database access layer (SQLite)
- `app/core/` - config, constants, and scheduling utilities
- `app/ui/` - shared UI/report helpers, keyboards, and UI text
- `app/infrastructure/` - middleware and integration glue
