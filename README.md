# Telegram Forwarder

A local-first Telegram campaign manager for forwarding authorized messages to groups, channels, and forum topics. Campaigns can use fixed message links, Saved Messages, private sources, or the newest post from a saved source.

## Features

- Create, edit, start, pause, resume, stop, and permanently remove ads
- Use messages from public channels, private groups, forum topics, or Saved Messages
- Scan the connected account for sendable destinations and available forum topics
- Select reusable destinations and maintain per-ad destination lists
- Configure message intervals, active days, schedule windows, quiet hours, and daily limits
- Coordinate overlapping campaigns so destination slow mode does not block unrelated groups
- Persist destination cooldowns and retry temporary Telegram failures
- Detect inaccessible destinations and media restrictions without stopping the whole campaign
- Track per-ad delivery statistics, recent activity, failures, and destination health
- Operate through the terminal or an authorized private Telegram control bot
- Keep account sessions, credentials, campaign data, logs, and exports outside the repository

## Requirements

- Python 3.10 or newer; Python 3.12 is the deployment baseline
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- A Telegram user account allowed to post in the selected destinations
- Optional BotFather bot for private remote controls

## Install

```bash
git clone https://github.com/dev-nic-codes/telegram-forwarder.git
cd telegram-forwarder
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

Windows PowerShell:

```powershell
git clone https://github.com/dev-nic-codes/telegram-forwarder.git
cd telegram-forwarder
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.ps1
```

The first interactive run creates the local data directory and guides account setup. Telegram credentials and login sessions are never read from this repository.

## Data directory

Writable data is stored below:

- Windows: `%APPDATA%\TelegramForwarder\data`
- Linux default: `~/TelegramForwarder/data`
- Service deployment: `$APPDATA/TelegramForwarder/data`

The directory contains Telegram sessions, account credentials, campaigns, destinations, bot configuration, logs, history, and exports. Back it up privately and never commit it.

## Running

Interactive terminal:

```bash
./start.sh
```

Unattended service runtime:

```bash
.venv/bin/python run_service.py --allow-risky
```

`--allow-risky` permits previously reviewed campaigns to run unattended; it does not bypass Telegram permissions, FloodWaits, or destination limits.

## Private control bot

Configure the BotFather token and authorized control chat through the interactive menu. The bot validates authorization in every command, callback, and text-state handler. Group messages are ignored by the control plane.

The menu supports ad creation and deletion, source and destination selection, per-ad scheduling and pacing, statistics, runtime logs, group synchronization, forum-topic selection, and service controls.

## Reliability model

The Forwarder separates campaign scheduling from delivery coordination:

- destination locks prevent simultaneous sends to the same group;
- group-specific slow mode is persisted and does not pause unrelated destinations;
- account-wide FloodWaits are applied only when Telegram provides account-level evidence;
- temporary failures remain retryable instead of silently advancing campaign state;
- inaccessible or incompatible destinations can be removed without stopping other targets;
- sessions and campaign state survive restarts;
- systemd can restart an unexpected process failure automatically.

## Tests

Run the complete suite:

```bash
python scripts/check.py
```

Lint the repository:

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
```

Tests use temporary runtime directories and do not require a Telegram login.

## Linux service

Example files are provided in [`deploy`](deploy/). Adjust the install path and service user before enabling the unit. Complete first-time Telegram login interactively using the same `APPDATA` value and Unix user that the service will use.

## Safety

Use the Forwarder only in chats where the account has permission to post. Telegram controls account limits, FloodWaits, slow mode, paid-message requirements, and access. The application respects these responses but cannot remove platform restrictions.

## License

Copyright (c) 2026 Nic. All rights reserved. This repository is source-available, not open source. See [LICENSE](LICENSE).
