"""Create a new production DB copy; the source is never modified."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

import db_layer


LEGACY_ACCOUNTANTS = {
    5178201242: ("ВП-16", "Е21", "ВЮ3"),
    1332732213: ("ОКПТ",),
}
LEGACY_VIEWERS = {
    5186498707: "Сокол Микола Миколайович",
    396484643: "Денисюк Станіслав Станіславович",
}
ORGANIZATIONS = (
    ("ВП-16", "osbb"),
    ("Е21", "osbb"),
    ("ОКПТ", "cooperative"),
    ("ВЮ3", "osbb"),
)


def migrate(source: Path, target: Path, superadmin_id: int, backup: Path | None = None) -> None:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    backup = (backup or target.with_name(f"{target.stem}.pre-migration{target.suffix}")).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Legacy database was not found: {source}")
    if Path(f"{source}-wal").exists() and Path(f"{source}-wal").stat().st_size:
        raise RuntimeError("Source has a non-empty SQLite WAL. Stop the old bot and checkpoint/backup the DB before migration.")
    if target.exists():
        raise FileExistsError(f"Target already exists; refusing to overwrite it: {target}")
    if source == target or source == backup or target == backup:
        raise ValueError("Source, target, and backup must be different paths")

    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(source, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

    tracked_tables = ("acts", "docs", "salaries", "jobs", "audit_log")
    with sqlite3.connect(target) as snapshot:
        before = {table: int(snapshot.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]) for table in tracked_tables}
        retired = {}
        for table in tracked_tables:
            columns = {r[1] for r in snapshot.execute(f'PRAGMA table_info("{table}")')}
            retired[table] = int(snapshot.execute(f'SELECT count(*) FROM "{table}" WHERE osbb=?', ("В19",)).fetchone()[0]) if "osbb" in columns else 0

    db_layer.DB_PATH = target
    db_layer.init_db_sync(superadmin_id)
    with db_layer.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO branches(name,owner_type,owner_user_id,status,payment_status) VALUES(?,?,?,?,?)",
            ("Існуюча гілка", "admin", int(superadmin_id), "active", "paid"),
        )
        branch_id = int(conn.execute("SELECT id FROM branches WHERE name=?", ("Існуюча гілка",)).fetchone()[0])
        conn.execute("UPDATE branches SET owner_user_id=? WHERE id=?", (int(superadmin_id), branch_id))

        # Remove only the retired V19 rows from the target copy. The source and
        # its rollback backup remain untouched until cutover is confirmed.
        for table in tracked_tables:
            columns = {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if "osbb" in columns:
                conn.execute(f'DELETE FROM "{table}" WHERE osbb=?', ("В19",))

        # A legacy "Акт оплачений" row has no amount or payment ledger. Only
        # completed acts are considered paid under the agreed migration rule.
        legacy_paid = conn.execute("SELECT id,osbb FROM acts WHERE status='Акт оплачений'").fetchall()
        for act in legacy_paid:
            conn.execute("UPDATE acts SET status='В роботі',updated_at=datetime('now') WHERE id=?", (act["id"],))
            conn.execute(
                "INSERT INTO item_events(item_type,item_id,event_type,old_value,new_value,comment,actor_id,created_at) VALUES('act',?,'legacy_status_migration','Акт оплачений','В роботі','No payment amount or payment history existed in the legacy database.',?,datetime('now'))",
                (act["id"], int(superadmin_id)),
            )

        org_ids: dict[str, int] = {}
        for name, kind in ORGANIZATIONS:
            conn.execute(
                "INSERT OR IGNORE INTO organizations(branch_id,name,kind,active,apartment_count,resident_code,leadership_code) VALUES(?,?,?,?,?,?,?)",
                (branch_id, name, kind, 1, 0, db_layer.create_join_code(), db_layer.create_join_code()),
            )
            org_ids[name] = int(conn.execute("SELECT id FROM organizations WHERE branch_id=? AND name=?", (branch_id, name)).fetchone()[0])

        def add_user(telegram_id: int, full_name: str) -> None:
            conn.execute(
                "INSERT INTO users(telegram_id,full_name) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET full_name=CASE WHEN users.full_name='' THEN excluded.full_name ELSE users.full_name END",
                (int(telegram_id), full_name),
            )

        def grant(telegram_id: int, org_id: int, role: str, keywords: str = "") -> None:
            conn.execute(
                "INSERT OR IGNORE INTO memberships(telegram_id,organization_id,role,keywords) VALUES(?,?,?,?)",
                (int(telegram_id), org_id, role, keywords),
            )

        add_user(superadmin_id, "Superadmin")
        for org_id in org_ids.values():
            grant(superadmin_id, org_id, "admin")
        for user_id, orgs in LEGACY_ACCOUNTANTS.items():
            add_user(user_id, "Бухгалтер")
            for name in orgs:
                grant(user_id, org_ids[name], "accountant")
        # Existing viewers are retained as user records only. Superadmin will
        # assign an organization and keywords through the admin menu.
        for user_id, full_name in LEGACY_VIEWERS.items():
            add_user(user_id, full_name)

        # Convert the hard-coded old payroll rates into editable database rows.
        legacy_rates = {
            "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання (Марія)": 9500,
                      "Прибирання (Олег)": 3000, "Сантехнік": 2800},
            "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": (4500, {"4-10": 3500, "11-3": 4500})},
            "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000,
                     "Двірник": 2000, "Обхідник": 1000, "Баки": 1000},
        }
        for name, rates in legacy_rates.items():
            for employee, amount in rates.items():
                seasonal_json = ""
                if isinstance(amount, tuple):
                    amount, seasonal = amount
                    import json
                    seasonal_json = json.dumps(seasonal, ensure_ascii=False)
                conn.execute(
                    "INSERT OR IGNORE INTO salary_templates(organization_id,employee,amount,seasonal_json) VALUES(?,?,?,?)",
                    (org_ids[name], employee, amount, seasonal_json),
                )

        conn.execute(
            "INSERT INTO audit_log(user_id,action,entity_type,entity_id,osbb,details,created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
            (int(superadmin_id), "legacy_migration", "branch", branch_id, None,
             "Imported legacy records; excluded retired organization В19; created new organization ВЮ3."),
        )
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,datetime('now'))")

    with sqlite3.connect(target) as verify:
        verify.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Migrated database failed integrity_check: {integrity}")
        for table in tracked_tables:
            cols = {r[1] for r in verify.execute(f'PRAGMA table_info("{table}")')}
            if "osbb" in cols and verify.execute(f'SELECT 1 FROM "{table}" WHERE osbb=? LIMIT 1', ("В19",)).fetchone():
                raise RuntimeError(f"V19 rows remain in migrated table {table}")
            expected = before[table] - retired[table] + (1 if table == "audit_log" else 0)
            actual = int(verify.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            if actual != expected:
                raise RuntimeError(f"Unexpected row count in {table}: expected {expected}, found {actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Supplied legacy osbb_acts.db")
    parser.add_argument("--target", required=True, type=Path, help="New production database path")
    parser.add_argument("--superadmin-id", required=True, type=int)
    parser.add_argument("--backup", type=Path, help="Optional explicit path for the untouched source backup")
    args = parser.parse_args()
    migrate(args.source, args.target, args.superadmin_id, args.backup)
    print(f"Migrated database created: {args.target.resolve()}")


if __name__ == "__main__":
    main()
