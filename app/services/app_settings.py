"""Ajustes globales de la app (guardados en Mongo, con respaldo en variables de entorno)."""
from __future__ import annotations

from typing import Any

from pymongo.database import Database

from app.config import get_settings

_KEY = "app"


def defaults() -> dict[str, Any]:
    s = get_settings()
    return {"auto_release_enabled": True, "auto_release_hours": s.auto_release_hours}


def get_app_settings(db: Database) -> dict[str, Any]:
    """Ajustes vigentes. Si nadie guardó nada aún, manda el valor de entorno."""
    doc = db.app_settings.find_one({"_id": _KEY}) or {}
    out = defaults()
    if "auto_release_enabled" in doc:
        out["auto_release_enabled"] = bool(doc["auto_release_enabled"])
    if "auto_release_hours" in doc:
        out["auto_release_hours"] = int(doc["auto_release_hours"])
    return out


def save_app_settings(db: Database, data: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "auto_release_enabled": bool(data["auto_release_enabled"]),
        "auto_release_hours": int(data["auto_release_hours"]),
    }
    db.app_settings.update_one({"_id": _KEY}, {"$set": clean}, upsert=True)
    return clean
