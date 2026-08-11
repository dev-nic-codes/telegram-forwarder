# Operations

## First login

Run `python run.py` interactively as the same operating-system user and with the same `APPDATA` value used by the service. Add the Telegram API credentials, complete login, choose the active account, and configure the optional private control bot.

## Service checks

```bash
systemctl status telegram-forwarder
journalctl -u telegram-forwarder -n 100 --no-pager
```

## Backups

Stop the service before copying the writable data directory. Protect the backup as a secret because it contains Telegram sessions and API credentials.

## Updates

Test a new checkout before replacing production code. Keep the writable data directory outside the checkout so source updates do not overwrite sessions or campaigns.

## Recovery

- Network interruption: the service reconnects and resumes retryable work.
- Destination slow mode: other destinations continue while the affected destination waits.
- Account FloodWait: the account resumes after Telegram's exact deadline.
- Revoked session: interactive login is required; session authorization cannot be repaired automatically.
- Invalid destination: remove or rescan it through the control menu.
