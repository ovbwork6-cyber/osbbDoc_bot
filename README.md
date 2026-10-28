# OSBB Telegram bot

Aiogram bot for OSBBs and housing cooperatives. It keeps the original SQLite business tables and adds role, apartment, meter, document, payment-history, and audit data.

## Roles and deployment settings

- Set `BOT_TOKEN` to the **same Telegram bot token** used by the old deployment. Existing Telegram file IDs depend on it.
- `SUPERADMIN_ID` defaults to `701568112` in the example. It can be overridden in `/etc/osbbbot.env`.
- Existing assignments in the migration: accountant `5178201242` for ВП-16, Е21, ВЮ3; accountant `1332732213` for ОКПТ. Legacy Viewer accounts remain without organization or keyword assignments.
- Existing ВП-16, Е21, and ОКПТ records are retained. V19 rows are excluded from the new DB. ВЮ3 is created as a new, empty organization.

## Local setup

Use Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and set BOT_TOKEN and OSBB_DB_PATH
.\.venv\Scripts\python.exe bot.py
```

## Import the supplied database

The migration never modifies its source. It makes a byte-for-byte backup at the specified `--backup` location, creates the target by copying the source, adds tables and columns, seeds the initial branch and roles, and removes V19 records from the target copy only. It refuses a non-empty `-wal` sidecar so uncheckpointed writes cannot be silently skipped.

```powershell
python migrate_legacy.py `
  --source D:\Victor\prog\recovery\osbb_acts.db `
  --target data\osbb_acts.db `
  --backup data\osbb_acts.pre-migration.db `
  --superadmin-id 701568112
```

Stop the old bot and checkpoint its database before copying it to the migration host. Do not run this command against a production DB that already exists; it intentionally refuses to overwrite a target. Keep both the source and backup until the new deployment has been confirmed.

## DigitalOcean Ubuntu deployment

The bot is a single polling process and does not need a web server. The unit in `deploy/osbb-bot.service` runs it under systemd. Keep secrets and the live database outside the Git checkout.

1. Install Python 3.12, `sqlite3`, `git`, and `gzip`; create an `osbb-bot` service account.
2. Check out this repository to `/opt/osbbDoc_bot`; create `/var/lib/osbb` and `/var/backups/osbb`, owned by `osbb-bot`, mode `0700`.
3. Create `/etc/osbbbot.env` from `.env.example`, set the existing `BOT_TOKEN`, `SUPERADMIN_ID=701568112`, and `OSBB_DB_PATH=/var/lib/osbb/osbb_acts.db`; set mode `0600`.
4. Create the production DB from the preserved source using `migrate_legacy.py`, then copy the resulting DB to `/var/lib/osbb/osbb_acts.db`. Keep the separate migration backup until the cutover is accepted.
5. Install dependencies in `/opt/osbbDoc_bot/.venv`, copy `deploy/osbb-bot.service` to `/etc/systemd/system/`, copy the backup service and timer there too, then run `systemctl daemon-reload`, `systemctl enable --now osbb-db-backup.timer`, and (at cutover) `systemctl enable --now osbb-bot.service`.
6. Verify `/start`, organization/role access, act and receipt workflows, resident readings, XLSX export, and a consistent DB backup. Only one process can poll a Telegram bot token; stop the old poller before starting this service.

The current Droplet has 512 MB RAM. Keep only this bot on it, enable a 1 GB swap file, and watch memory during XLSX generation and ZIP reports. DigitalOcean backups should also be enabled, and a copy of database backups should be stored off the Droplet.

The web app on the Droplet is not removed by this repository. Remove it during the approved cutover after the new bot and migrated data have been verified.

## Operational notes

- The data migration preserves legacy row IDs and file IDs. Legacy acts with status `Акт оплачений` but no payment evidence are moved to `В роботі`; their previous status is recorded in `item_events`. Other completed records are left untouched.
- New acts require an amount and performer. The `/actamount` command can add a missing total to an open legacy act; completed acts and their existing records remain unchanged.
- Apartment keys are intentionally stored in the DB so Admin and Accountant can export them. Protect both the production DB and key workbooks as sensitive data.
- `systemd` restarts the bot after failure. The backup timer stores 14 days of compressed SQLite online backups on the Droplet; copy them off-host for disaster recovery.
