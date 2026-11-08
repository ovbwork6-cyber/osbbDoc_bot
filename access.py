"""Role and organization access checks shared by all message and callback paths."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import db_layer

SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID") or os.getenv("CHAIRMAN_ID") or "0")


def active_mode(telegram_id: int) -> str:
    if int(telegram_id) != SUPERADMIN_ID or not SUPERADMIN_ID:
        return "organization"
    row = db_layer.fetch_one("SELECT mode FROM user_modes WHERE telegram_id=?", (int(telegram_id),))
    return row["mode"] if row and row["mode"] in {"superadmin", "organization"} else "superadmin"


def set_active_mode(telegram_id: int, mode: str) -> None:
    if int(telegram_id) != SUPERADMIN_ID or mode not in {"superadmin", "organization"}:
        raise ValueError("Invalid mode switch")
    db_layer.execute(
        "INSERT INTO user_modes(telegram_id,mode,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(telegram_id) DO UPDATE SET mode=excluded.mode,updated_at=CURRENT_TIMESTAMP",
        (int(telegram_id), mode),
    )


def roles_for(telegram_id: int, organization: str | None = None) -> set[str]:
    if is_superadmin(telegram_id):
        return {"superadmin"}
    sql = "SELECT m.role FROM memberships m JOIN organizations o ON o.id=m.organization_id JOIN branches b ON b.id=o.branch_id WHERE m.telegram_id=? AND m.active=1 AND o.active=1 AND b.status='active'"
    params: list[Any] = [int(telegram_id)]
    if organization:
        sql += " AND o.name=?"
        params.append(organization)
    return {row["role"] for row in db_layer.fetch_all(sql, params)}


def memberships_for(telegram_id: int) -> list[dict[str, Any]]:
    if is_superadmin(telegram_id):
        return []
    return db_layer.fetch_all(
        """SELECT DISTINCT o.id,o.name,o.kind,o.branch_id FROM organizations o
           JOIN memberships m ON m.organization_id=o.id
           JOIN branches b ON b.id=o.branch_id
           WHERE m.telegram_id=? AND m.active=1 AND o.active=1 AND b.status='active'
           ORDER BY o.name""",
        (int(telegram_id),),
    )


def organization_state(name: str) -> dict[str, Any] | None:
    return db_layer.fetch_one(
        """SELECT o.*,b.status AS branch_status,b.payment_status FROM organizations o
           JOIN branches b ON b.id=o.branch_id WHERE o.name=?""",
        (name,),
    )


def _paid_for_writes(row: dict[str, Any]) -> bool:
    if row["payment_status"] not in {"paid", "trial"}:
        return False
    if row.get("paid_until"):
        try:
            return date.fromisoformat(row["paid_until"]) >= datetime.now(ZoneInfo("Europe/Kyiv")).date()
        except ValueError:
            return False
    return True


def can_access_osbb(telegram_id: int, organization: str) -> bool:
    """Read access remains available during unpaid/blocked service states."""
    row = organization_state(organization)
    if not row:
        return False
    return bool(row["active"] and row["branch_status"] == "active" and roles_for(telegram_id, organization))


def can_manage_osbb(telegram_id: int, organization: str) -> bool:
    row = organization_state(organization)
    if not row:
        return False
    if not row["active"] or row["branch_status"] != "active": return False
    if not _paid_for_writes(row):
        return False
    return bool(roles_for(telegram_id, organization) & {"admin", "fop"})


def can_process_osbb(telegram_id: int, organization: str) -> bool:
    row = organization_state(organization)
    if not row:
        return False
    if is_superadmin(telegram_id): return False
    if not row["active"] or row["branch_status"] != "active": return False
    if not _paid_for_writes(row):
        return False
    return "accountant" in roles_for(telegram_id, organization)


def can_read_org_data(telegram_id: int, organization: str) -> bool:
    return can_access_osbb(telegram_id, organization)


def is_superadmin(telegram_id: int) -> bool:
    return bool(SUPERADMIN_ID and int(telegram_id) == SUPERADMIN_ID and active_mode(telegram_id) == "superadmin")


def is_chairman(telegram_id: int) -> bool:
    """Whether the user has an organization-level Admin or FOP role."""
    return not is_superadmin(telegram_id) and bool(roles_for(telegram_id) & {"admin", "fop"})


def is_readonly_viewer(telegram_id: int) -> bool:
    if is_superadmin(telegram_id):
        return False
    return bool(db_layer.fetch_one("SELECT 1 FROM memberships WHERE telegram_id=? AND role='viewer' AND active=1 LIMIT 1", (int(telegram_id),)))


def readonly_keywords(telegram_id: int, organization: str | None = None) -> list[str]:
    sql = "SELECT keywords FROM memberships m JOIN organizations o ON o.id=m.organization_id WHERE m.telegram_id=? AND m.role='viewer' AND m.active=1"
    params: list[Any] = [int(telegram_id)]
    if organization:
        sql += " AND o.name=?"
        params.append(organization)
    values = [r["keywords"] for r in db_layer.fetch_all(sql, params)]
    return [piece.strip() for value in values for piece in value.split(",") if piece.strip()]


def readonly_name(telegram_id: int) -> str:
    row = db_layer.fetch_one("SELECT full_name FROM users WHERE telegram_id=?", (int(telegram_id),))
    return row["full_name"] if row else "Користувач"


def user_allowed_osbbs(telegram_id: int) -> list[str]:
    if is_superadmin(telegram_id):
        return []
    return [r["name"] for r in memberships_for(telegram_id)]
