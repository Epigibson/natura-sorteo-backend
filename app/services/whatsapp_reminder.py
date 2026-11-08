"""Recordatorio masivo por WhatsApp vía Evolution API."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx
from pymongo.database import Database

from app.config import get_settings
from app.services.raffle_service import audit, digits, get_raffle, now_utc, oid

MASS_REMIND_ACTION = "mass_remind"
COOLDOWN_HOURS = 24
DELAY_SECONDS = 1.5

_ALLOWED_STATUSES = frozenset({"open", "closed", "drawing"})


def collect_unique_phones(db: Database, raffle_id: str) -> list[str]:
    """Teléfonos únicos de 10 dígitos del sorteo, en orden estable (sorted)."""
    rid = oid(raffle_id)
    phones: set[str] = set()
    for t in db.tickets.find({"raffle_id": rid}, {"participant.phone": 1}):
        d = digits((t.get("participant") or {}).get("phone"))
        if len(d) == 10:
            phones.add(d)
    return sorted(phones)


def _count_skipped_no_phone(db: Database, raffle_id: str) -> int:
    """Tickets con participante pero sin teléfono usable (10 dígitos)."""
    rid = oid(raffle_id)
    n = 0
    for t in db.tickets.find({"raffle_id": rid, "participant": {"$ne": None}}, {"participant.phone": 1}):
        d = digits((t.get("participant") or {}).get("phone"))
        if len(d) != 10:
            n += 1
    return n


def build_reminder_message(raffle: dict, base_url: str) -> str:
    title = raffle.get("title") or ""
    prize = raffle.get("prize") or ""
    draw_date = raffle.get("draw_date") or "por definir"
    slug = raffle.get("slug") or ""
    board = f"{(base_url or '').rstrip('/')}/sorteo/{slug}/tablero"
    return (
        f"Hola, recordatorio 🎲\n"
        f'El sorteo "{title}" ({prize}) se realiza el {draw_date}.\n'
        f"Revisa el tablero: {board}"
    )


def wa_jid(phone_10: str) -> str:
    return "52" + phone_10


def send_text_evolution(*, url: str, api_key: str, instance: str, jid: str, text: str) -> None:
    endpoint = f"{url.rstrip('/')}/message/sendText/{instance}"
    try:
        r = httpx.post(
            endpoint,
            headers={"apikey": api_key},
            json={"number": jid, "text": text},
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Evolution HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Evolution error: {exc}") from exc


def _require_config() -> tuple[str, str, str]:
    s = get_settings()
    if not s.evolution_url:
        raise ValueError("WhatsApp no está configurado (EVOLUTION_URL vacío)")
    return s.evolution_url, s.evolution_api_key, s.evolution_instance


def send_mass_remind(
    db: Database,
    raffle_id: str,
    *,
    by: str,
    base_url: str,
    now: Any = None,
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    if raffle.get("status") not in _ALLOWED_STATUSES:
        raise ValueError("Este sorteo no admite recordatorios masivos")

    url, api_key, instance = _require_config()

    moment = now or now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now_utc().tzinfo)
    cutoff = moment - timedelta(hours=COOLDOWN_HOURS)
    prior = db.audit_log.find_one({
        "action": MASS_REMIND_ACTION,
        "raffle_id": str(raffle_id),
        "at": {"$gte": cutoff},
    })
    if prior:
        raise ValueError(f"Ya se envió un recordatorio en las últimas {COOLDOWN_HOURS} horas")

    phones = collect_unique_phones(db, raffle_id)
    if not phones:
        raise ValueError("No hay participantes con teléfono")

    message = build_reminder_message(raffle, base_url)
    sent = failed = 0
    for i, phone in enumerate(phones):
        try:
            send_text_evolution(
                url=url, api_key=api_key, instance=instance,
                jid=wa_jid(phone), text=message,
            )
            sent += 1
        except Exception:
            failed += 1
        if i < len(phones) - 1:
            time.sleep(DELAY_SECONDS)

    skipped_no_phone = _count_skipped_no_phone(db, raffle_id)
    recipients = len(phones)
    audit(
        db, MASS_REMIND_ACTION, by=by, raffle_id=raffle_id,
        sent=sent, failed=failed, recipients=recipients,
        skipped_no_phone=skipped_no_phone,
    )
    return {
        "sent": sent,
        "failed": failed,
        "skipped_no_phone": skipped_no_phone,
        "recipients": recipients,
    }
