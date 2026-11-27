"""Ajustes globales de la app (guardados en Mongo, con respaldo en variables de entorno)."""
from __future__ import annotations

from typing import Any

from pymongo.database import Database

from app.config import get_settings

_KEY = "app"
_MKT_KEY = "marketing"


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


def get_marketing_settings(db: Database) -> dict[str, Any]:
    """Ajustes de automatización de marketing (sin exponer secretos)."""
    doc = db.app_settings.find_one({"_id": _MKT_KEY}) or {}
    s = get_settings()
    out: dict[str, Any] = {
        "auto_wa_on_pay": True,
        "auto_wa_on_draw": True,
        "auto_fb_on_create": False,
        "auto_fb_milestones": True,
        "public_base_url": (s.public_base_url or "").strip().rstrip("/"),
        "fb_page_id": (s.fb_page_id or "").strip(),
        "fb_token_configured": bool((s.fb_page_access_token or "").strip()),
        "evolution_configured": bool((s.evolution_url or "").strip()),
        "wa_vip_group_jid": "",
        "wa_vip_group_invite_url": "",
    }
    for key in ("auto_wa_on_pay", "auto_wa_on_draw", "auto_fb_on_create", "auto_fb_milestones"):
        if key in doc:
            out[key] = bool(doc[key])
    if "public_base_url" in doc:
        out["public_base_url"] = str(doc["public_base_url"] or "").strip().rstrip("/")
    if "fb_page_id" in doc and str(doc["fb_page_id"] or "").strip():
        out["fb_page_id"] = str(doc["fb_page_id"]).strip()
    if "wa_vip_group_jid" in doc:
        out["wa_vip_group_jid"] = str(doc["wa_vip_group_jid"] or "").strip()
    if "wa_vip_group_invite_url" in doc:
        out["wa_vip_group_invite_url"] = str(doc["wa_vip_group_invite_url"] or "").strip()
    has_db_token = bool(str(doc.get("fb_page_access_token") or "").strip())
    out["fb_token_configured"] = has_db_token or bool(str(s.fb_page_access_token or "").strip())
    return out


def save_marketing_settings(db: Database, data: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {
        "auto_wa_on_pay": bool(data.get("auto_wa_on_pay", True)),
        "auto_wa_on_draw": bool(data.get("auto_wa_on_draw", True)),
        "auto_fb_on_create": bool(data.get("auto_fb_on_create", False)),
        "auto_fb_milestones": bool(data.get("auto_fb_milestones", True)),
        "public_base_url": str(data.get("public_base_url") or "").strip().rstrip("/"),
        "fb_page_id": str(data.get("fb_page_id") or "").strip(),
        "wa_vip_group_jid": str(data.get("wa_vip_group_jid") or "").strip(),
        "wa_vip_group_invite_url": str(data.get("wa_vip_group_invite_url") or "").strip(),
    }
    raw_token = data.get("fb_page_access_token")
    if raw_token is not None:
        token_str = str(raw_token).strip()
        if token_str:
            clean["fb_page_access_token"] = token_str
        else:
            db.app_settings.update_one({"_id": _MKT_KEY}, {"$unset": {"fb_page_access_token": ""}})
    db.app_settings.update_one({"_id": _MKT_KEY}, {"$set": clean}, upsert=True)
    return get_marketing_settings(db)


def get_fb_credentials(db: Database) -> tuple[str, str, str]:
    """Devuelve (page_id, access_token, public_base_url) combinando DB y env."""
    doc = db.app_settings.find_one({"_id": _MKT_KEY}) or {}
    s = get_settings()
    page_id = str(doc.get("fb_page_id") or "").strip() or (s.fb_page_id or "").strip()
    token = str(doc.get("fb_page_access_token") or "").strip() or (s.fb_page_access_token or "").strip()
    base_url = str(doc.get("public_base_url") or "").strip().rstrip("/") or (s.public_base_url or "").strip().rstrip("/")
    return page_id, token, base_url
