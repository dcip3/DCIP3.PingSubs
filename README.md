<p align="center">
  <img src="docs/assets/pingsubs.jpg" alt="PingSubs icon" width="128" height="128">
</p>
<h1 align="center">PingSubs</h1>

A self-hosted Telegram bot for shared subscriptions, payment reminders, and member balances.
Built with Python 3.13, aiogram, and SQLite.

- Split subscription costs across members, with individual shares and currencies.
- Send scheduled reminders, track payment confirmations, and view payment reports.
- Invite members through personal links and manage balance top-ups with admin approval.

Payments happen outside the bot; PingSubs records them.

## Deploy with Docker

Requires Git, Docker with Compose, and a Telegram bot token from **@BotFather**.

```bash
git clone https://github.com/dcip3/DCIP3.PingSubs.git
cd DCIP3.PingSubs
cp .env.example .env
```

In `.env`, set `BOT_TOKEN` and put your numeric Telegram user ID into `ADMIN_IDS`.
**Set the admin ID before the first launch:** with a fresh database and no configured
admins, the first person to send `/start` becomes the administrator.

```bash
docker compose up -d --build
docker compose logs -f --tail=100
```

The bot uses long polling: no domain, HTTPS certificate, or inbound port is needed.
SQLite data is stored in `./data/` and survives container recreation. Keep the default
`DATABASE_PATH` when using the included Compose file so it stays inside the mounted directory.

To update an existing installation:

```bash
git pull --ff-only
docker compose up -d --build
```

Stop with `docker compose down`. Back up `data/` while the bot is stopped, and keep
`.env` separately. Run only one bot instance per token.

<details>
<summary>Optional: GitHub Actions deployment</summary>

[deploy.yml](.github/workflows/deploy.yml) deploys `main` over SSH after the
repository checks pass. Prepare a working installation at `/opt/DCIP3.PingSubs` on
the server and configure the repository secrets `VDS_HOST`, `VDS_USER`, and
`VDS_SSH_KEY`. The SSH user needs access to that directory, Git, and Docker; `.env`
and `data/` remain on the server. The deployment resets the server checkout to
`origin/main`, so keep local changes out of that directory. For a private
repository, the server also needs GitHub read access.

</details>

## Run locally

Requires **Python 3.13**. Clone the repository and configure `.env` as above, then:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

</details>

## Configuration

Settings are read from environment variables or `.env`. Existing environment
variables take precedence. The data directory is created automatically.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | Required | Telegram bot token from @BotFather |
| `ADMIN_IDS` | Empty | Admin Telegram user IDs, separated by commas; set before first launch |
| `DATABASE_PATH` | `data/app.db` | SQLite database path |
| `REMINDER_CHECK_INTERVAL` | `60` | Seconds between reminder checks, clamped to 5-60 |
| `BASE_REMINDER_TIME` | `16:00` | Default reminder time, `HH:MM` |
| `BASE_TIMEZONE` | `Europe/Moscow` | Default IANA timezone, e.g. `Europe/Belgrade` |
| `TARGET_CURRENCY` | `RUB` | Default currency for conversions |
| `CURRENCY_ROUNDING` | `precise` | `precise`, `floor`, `round`, or `ceil` |

Rounding, base reminder time, and timezone seed a new database and can be changed
later in the admin **Settings**; saved values take precedence over the environment on
later starts. `TARGET_CURRENCY` is stored on the first launch only; to change it later,
update the `target_currency` row in the `settings` table of the database.

Cross-currency amounts use exchange rates from [open.er-api.com](https://www.exchangerate-api.com)
(the ExchangeRate-API open endpoint), fetched over outbound HTTPS and cached for six
hours. It is the only external service besides Telegram. When it is unreachable,
converted amounts are left out and only the original currency is shown.

## Using the bot

Send `/start` from an admin account, add members, and share their personal invite
links. Create a subscription, assign members, and set its amount, billing period,
and reminder schedule. Members can then view their account and confirm payments.

In **Settings**, users can choose their reminder time and timezone; a display
currency can be picked per subscription from the subscription card.
Reminders follow each recipient's timezone; subscription-specific times override
base settings. Balance top-ups are credited after an admin confirms the transfer.

[Contributing & checks](CONTRIBUTING.md) · [Security](SECURITY.md)
