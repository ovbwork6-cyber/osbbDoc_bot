"""Role and organization access checks shared by all message and callback paths."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import db_layer

SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID") or os.getenv("CHAIRMAN_ID") or "0")


def roles_for(telegram_id: int, organization: str | None = None) -> set[str]:
    if int(telegram_id) == SUPERADMIN_ID and SUPERADMIN_ID:
        return {"superadmin", "admin"}
    sql = "SELECT m.role FROM memberships m JOIN organizations o ON o.id=m.organization_id JOIN branches b ON b.id=o.branch_id WHERE m.telegram_id=? AND m.active=1 AND o.active=1 AND b.status='active'"
    params: list[Any] = [int(telegram_id)]
    if organization:
        sql += " AND o.name=?"
        params.append(organization)
    return {row["role"] for row in db_layer.fetch_all(sql, params)}


def memberships_for(telegram_id: int) -> list[dict[str, Any]]:
    if int(telegram_id) == SUPERADMIN_ID and SUPERADMIN_ID:
        return db_layer.fetch_all("SELECT id,name,kind,branch_id FROM organizations WHERE active=1 ORDER BY name")
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
    if int(telegram_id) == SUPERADMIN_ID and SUPERADMIN_ID:
        return True
    return bool(row["active"] and row["branch_status"] == "active" and roles_for(telegram_id, organization))


def can_manage_osbb(telegram_id: int, organization: str) -> bool:
    row = organization_state(organization)
    if not row:
        return False
    if int(telegram_id) == SUPERADMIN_ID and SUPERADMIN_ID:
        return True
    if not row["active"] or row["branch_status"] != "active": return False
    if not _paid_for_writes(row):
        return False
    return int(telegram_id) == SUPERADMIN_ID or bool(roles_for(telegram_id, organization) & {"admin", "fop"})


def can_process_osbb(telegram_id: int, organization: str) -> bool:
    row = organization_state(organization)
    if not row:
        return False
    if int(telegram_id) == SUPERADMIN_ID and SUPERADMIN_ID: return True
    if not row["active"] or row["branch_status"] != "active": return False
    if not _paid_for_writes(row):
        return False
    return "accountant" in roles_for(telegram_id, organization)


def can_read_org_data(telegram_id: int, organization: str) -> bool:
    return can_access_osbb(telegram_id, organization)


def is_superadmin(telegram_id: int) -> bool:
    return bool(SUPERADMIN_ID and int(telegram_id) == SUPERADMIN_ID)


def is_chairman(telegram_id: int) -> bool:
    """Compatibility name used by legacy handlers: Admin/FOP or Superadmin."""
    return is_superadmin(telegram_id) or bool(roles_for(telegram_id) & {"admin", "fop"})


def is_readonly_viewer(telegram_id: int) -> bool:
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
        return [r["name"] for r in db_layer.fetch_all("SELECT name FROM organizations WHERE active=1 ORDER BY name")]
    return [r["name"] for r in memberships_for(telegram_id)]
