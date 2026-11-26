"""Automatizaciones de marketing por WhatsApp (Evolution API)."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import get_settings
from app.services import app_settings as aps
from app.services import whatsapp_reminder as wr
from app.services.raffle_service import audit, digits, get_raffle, now_utc, oid

UNPAID_REMIND_ACTION = "unpaid_remind"
POST_DRAW_ACTION = "post_draw_campaign"
PAST_INVITE_ACTION = "past_invite"
UNPAID_COOLDOWN_HOURS = 12
PAST_INVITE_COOLDOWN_HOURS = 24
PAST_INVITE_DELAY_SECONDS = 4.5


def _effective_base_url(db: Database, base_url: str) -> str:
    clean = (base_url or "").strip().rstrip("/")
    if clean:
        return clean
    _, _, cfg_base = aps.get_fb_credentials(db)
    return cfg_base


def _send_batch(
    phones: list[str],
    message_for: Any,
    *,
    delay_seconds: float | None = None,
    stop_on_disconnect: bool = False,
) -> tuple[int, int]:
    """Envía mensajes uno a uno respetando pausa anti-ban de WhatsApp."""
    url, api_key, instance = wr._require_config()
    delay = wr.DELAY_SECONDS if delay_seconds is None else delay_seconds
    sent = failed = 0
    consecutive_errors = 0
    for i, phone in enumerate(phones):
        try:
            text = message_for(phone) if callable(message_for) else str(message_for)
            wr.send_text_evolution(
                url=url,
                api_key=api_key,
                instance=instance,
                jid=wr.wa_jid(phone),
                text=text,
            )
            sent += 1
            consecutive_errors = 0
        except Exception as exc:
            failed += 1
            consecutive_errors += 1
            err_txt = str(exc).lower()
            if stop_on_disconnect and (
                "428" in err_txt
                or "connection closed" in err_txt
                or "404" in err_txt
                or consecutive_errors >= 3
            ):
                # Cuenta los restantes como no enviados y corta para no quemar el número
                failed += len(phones) - i - 1
                break
        if i < len(phones) - 1 and delay > 0:
            time.sleep(delay)
    return sent, failed


def _claim_window_lock(
    db: Database,
    *,
    action: str,
    raffle_id: str,
    by: str,
    cooldown_hours: int | None,
) -> Any:
    """Reclama un candado atómico en audit_log antes de iniciar el lote."""
    moment = now_utc()
    query: dict[str, Any] = {"action": action, "raffle_id": str(raffle_id)}
    if cooldown_hours is not None:
        query["at"] = {"$gte": moment - timedelta(hours=cooldown_hours)}

    claim_id = uuid4().hex
    doc = db.audit_log.find_one_and_update(
        query,
        {
            "$setOnInsert": {
                "at": moment,
                "action": action,
                "by": by,
                "raffle_id": str(raffle_id),
                "status": "pending",
                "claim_id": claim_id,
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if doc.get("claim_id") != claim_id:
        if cooldown_hours is None:
            raise ValueError("Esta campaña ya se envió para este sorteo")
        raise ValueError(f"Ya se envió en las últimas {cooldown_hours} horas")
    return doc["_id"]


# ---------- 1) Auto-confirmación de pago + Referido 1+1 ----------
def build_payment_confirmed_message(raffle: dict, ticket: dict, base_url: str) -> str:
    p = ticket.get("participant") or {}
    name = str(p.get("name") or "").split(" ")[0] or "hola"
    folio = int(ticket.get("folio") or 0)
    amount = ticket.get("amount") or 0
    title = raffle.get("title") or "Sorteo Natura"
    draw_date = raffle.get("draw_date") or "en vivo próximamente"
    slug = raffle.get("slug") or ""
    landing = f"{base_url}/sorteo/{slug}" if base_url else ""

    msg = (
        f"¡Listo {name}! ✅ Tu folio #{folio:02d} (${amount} MXN) quedó confirmado en *{title}*.\n"
        f"📅 Sorteo: {draw_date}.\n\n"
        f"🌿 *Tip 1+1:* Si invitas a una amiga a apartar y pagar su boleto"
        + (f" aquí 👉 {landing}" if landing else "")
        + ", te regalo un *10% de descuento* en tu próximo pedido Natura. ¡Mucha suerte! 🍀"
    )
    return msg


def safe_notify_payment_confirmed(
    db: Database,
    raffle_id: str,
    ticket: dict,
    *,
    base_url: str = "",
) -> None:
    """Envía confirmación + referido 1+1 al cobrar un boleto (silencioso si falla o no hay config)."""
    try:
        s = get_settings()
        if not s.evolution_url:
            return
        if not aps.get_marketing_settings(db).get("auto_wa_on_pay", True):
            return
        phone = digits((ticket.get("participant") or {}).get("phone"))
        if len(phone) != 10:
            return
        raffle = get_raffle(db, raffle_id)
        if not raffle:
            return
        eff_base = _effective_base_url(db, base_url)
        text = build_payment_confirmed_message(raffle, ticket, eff_base)
        wr.send_text_evolution(
            url=s.evolution_url,
            api_key=s.evolution_api_key,
            instance=s.evolution_instance,
            jid=wr.wa_jid(phone),
            text=text,
        )
        audit(
            db,
            "wa_pay_confirm",
            by="auto",
            raffle_id=str(raffle_id),
            folio=ticket.get("folio"),
            status="ok",
        )
    except Exception as exc:
        audit(
            db,
            "wa_pay_confirm",
            by="auto",
            raffle_id=str(raffle_id),
            folio=ticket.get("folio"),
            status="error",
            error=str(exc)[:200],
        )


# ---------- 2) Recordatorio a apartados sin pagar ----------
def send_unpaid_reminders(
    db: Database,
    raffle_id: str,
    *,
    by: str,
    base_url: str = "",
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    if raffle.get("status") not in ("open", "closed"):
        raise ValueError("Este sorteo no admite recordatorios de pago")

    wr._require_config()
    eff_base = _effective_base_url(db, base_url)

    by_phone: dict[str, list[dict]] = {}
    skipped_no_phone = 0
    for t in db.tickets.find(
        {"raffle_id": oid(raffle_id), "status": {"$in": ["delivered", "registered", "scratched"]}}
    ):
        d = digits((t.get("participant") or {}).get("phone"))
        if len(d) != 10:
            skipped_no_phone += 1
            continue
        by_phone.setdefault(d, []).append(t)

    phones = sorted(by_phone.keys())
    if not phones:
        raise ValueError("No hay boletos pendientes de pago con teléfono")

    lock_id = _claim_window_lock(
        db,
        action=UNPAID_REMIND_ACTION,
        raffle_id=raffle_id,
        by=by,
        cooldown_hours=UNPAID_COOLDOWN_HOURS,
    )

    title = raffle.get("title") or "Sorteo Natura"
    slug = raffle.get("slug") or ""
    board = f"{eff_base}/sorteo/{slug}/tablero" if eff_base else ""

    def _msg(phone: str) -> str:
        tks = sorted(by_phone[phone], key=lambda x: x["folio"])
        name = str((tks[0].get("participant") or {}).get("name") or "").split(" ")[0] or "hola"
        folios_txt = ", ".join(f"#{t['folio']:02d}" for t in tks)
        total = sum(int(t.get("amount") or 0) for t in tks if t.get("status") == "scratched")
        total_line = f" (total raspado: ${total} MXN)" if total > 0 else ""
        return (
            f"Hola {name} 👋 Recordatorio amable de tu apartado en *{title}*:\n"
            f"🎟️ Folio(s): {folios_txt}{total_line}.\n"
            f"Envía tu comprobante de pago por aquí para asegurar tu lugar antes de que se libere ⏰"
            + (f"\nTablero: {board}" if board else "")
        )

    sent, failed = _send_batch(phones, _msg)
    if sent == 0:
        db.audit_log.delete_one({"_id": lock_id})
        raise ValueError(f"No se envió ningún recordatorio ({failed} fallos); revisa Evolution API")

    db.audit_log.update_one(
        {"_id": lock_id},
        {"$set": {"status": "sent", "sent": sent, "failed": failed, "recipients": len(phones)}},
    )
    return {
        "sent": sent,
        "failed": failed,
        "skipped_no_phone": skipped_no_phone,
        "recipients": len(phones),
    }


# ---------- 3) Post-sorteo: ganador + bono rescate a no ganadores ----------
def send_post_draw_campaign(
    db: Database,
    raffle_id: str,
    *,
    by: str,
    base_url: str = "",
    bonus_amount: int = 30,
    min_order: int = 350,
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    if raffle.get("status") != "drawn" or not raffle.get("winner"):
        raise ValueError("El sorteo aún no se ha realizado")

    wr._require_config()
    winner = raffle["winner"]
    winner_phone = digits((winner.get("participant") or {}).get("phone"))
    winner_folio = int(winner.get("folio") or 0)

    paid_tickets = list(db.tickets.find({"raffle_id": oid(raffle_id), "status": "paid"}))
    non_winner_phones: set[str] = set()
    names_by_phone: dict[str, str] = {}
    for t in paid_tickets:
        p = t.get("participant") or {}
        d = digits(p.get("phone"))
        if len(d) != 10:
            continue
        names_by_phone.setdefault(d, str(p.get("name") or "").split(" ")[0] or "hola")
        if d != winner_phone:
            non_winner_phones.add(d)

    all_phones = ([winner_phone] if len(winner_phone) == 10 else []) + sorted(non_winner_phones)
    if not all_phones:
        raise ValueError("No hay participantes con teléfono para notificar")

    lock_id = _claim_window_lock(
        db,
        action=POST_DRAW_ACTION,
        raffle_id=raffle_id,
        by=by,
        cooldown_hours=None,
    )

    title = raffle.get("title") or "Sorteo Natura"
    prize = raffle.get("prize") or "Premio Natura"

    def _msg(phone: str) -> str:
        first_name = names_by_phone.get(phone, "hola")
        if phone == winner_phone:
            return (
                f"🏆 ¡FELICIDADES {first_name.upper()}! 🎉\n"
                f"Ganaste *{prize}* en *{title}* con tu folio #{winner_folio:02d} 🍀✨\n"
                f"Escríbeme por aquí para coordinar la entrega de tu premio 💚"
            )
        return (
            f"¡Hola {first_name}! 🌿 Gracias por participar en *{title}*.\n"
            f"El premio se fue con el folio #{winner_folio:02d}, pero no te quedas sin regalo: "
            f"tienes un *bono de ${bonus_amount} MXN* de descuento en tu próximo pedido Natura "
            f"desde ${min_order} MXN esta semana 🎁\n"
            f"¿Te mando el catálogo actual?"
        )

    sent, failed = _send_batch(all_phones, _msg)
    if sent == 0:
        db.audit_log.delete_one({"_id": lock_id})
        raise ValueError(f"No se pudo enviar la campaña post-sorteo ({failed} fallos)")

    db.audit_log.update_one(
        {"_id": lock_id},
        {"$set": {"status": "sent", "sent": sent, "failed": failed, "recipients": len(all_phones)}},
    )
    return {
        "sent": sent,
        "failed": failed,
        "skipped_no_phone": 0,
        "recipients": len(all_phones),
    }


def safe_auto_post_draw(db: Database, raffle_id: str, *, base_url: str = "") -> None:
    """Dispara campaña post-sorteo en BackgroundTasks si auto_wa_on_draw está activo."""
    try:
        if not get_settings().evolution_url:
            return
        if not aps.get_marketing_settings(db).get("auto_wa_on_draw", True):
            return
        send_post_draw_campaign(db, raffle_id, by="auto", base_url=base_url)
    except Exception:
        pass


# ---------- 4) Invitación a ex-participantes ----------
def collect_past_participant_contacts(db: Database, raffle_id: str) -> dict[str, str]:
    """Mapa {teléfono: primer_nombre} de sorteos anteriores que aún no participan aquí."""
    rid = oid(raffle_id)
    current_phones: set[str] = set()
    for t in db.tickets.find({"raffle_id": rid}, {"participant.phone": 1}):
        d = digits((t.get("participant") or {}).get("phone"))
        if len(d) == 10:
            current_phones.add(d)

    contacts: dict[str, str] = {}
    for t in db.tickets.find({"raffle_id": {"$ne": rid}}, {"participant.phone": 1, "participant.name": 1}):
        p = t.get("participant") or {}
        d = digits(p.get("phone"))
        if len(d) == 10 and d not in current_phones and d not in contacts:
            first_name = str(p.get("name") or "").strip().split(" ")[0]
            contacts[d] = first_name
    return contacts


def collect_past_participant_phones(db: Database, raffle_id: str) -> list[str]:
    """Teléfonos únicos de sorteos anteriores que aún no participan en el sorteo actual."""
    return sorted(collect_past_participant_contacts(db, raffle_id).keys())


def send_past_participants_invite(
    db: Database,
    raffle_id: str,
    *,
    by: str,
    base_url: str = "",
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    if raffle.get("status") not in ("open", "closed"):
        raise ValueError("Solo se pueden invitar ex-participantes a sorteos abiertos o cerrados")

    wr._require_config()
    eff_base = _effective_base_url(db, base_url)
    contacts = collect_past_participant_contacts(db, raffle_id)
    phones = sorted(contacts.keys())
    if not phones:
        raise ValueError("No hay ex-participantes nuevos para invitar")

    lock_id = _claim_window_lock(
        db,
        action=PAST_INVITE_ACTION,
        raffle_id=raffle_id,
        by=by,
        cooldown_hours=PAST_INVITE_COOLDOWN_HOURS,
    )

    title = raffle.get("title") or "Sorteo Natura"
    prize = raffle.get("prize") or "Premio Natura"
    pmin = raffle.get("price_min") or 30
    pmax = raffle.get("price_max") or 50
    slug = raffle.get("slug") or ""
    landing = f"{eff_base}/sorteo/{slug}" if eff_base else ""

    def _msg(phone: str) -> str:
        name = contacts.get(phone, "")
        saludo = f"¡Hola {name}! 🌿" if name else "¡Hola! 🌿"
        return (
            f"{saludo} Ya abrió un nuevo raspadito Natura: *{title}* 🎟️\n"
            f"🎁 Premio: {prize}\n"
            f"💰 Tu boleto sale de ${pmin} a ${pmax} MXN al raspar (misma probabilidad para todos 🍀)\n"
            + (f"Escoge tu folio antes de que vuelen 👉 {landing}" if landing else "Escríbeme si quieres apartar folio 🙌")
        )

    sent, failed = _send_batch(
        phones,
        _msg,
        delay_seconds=PAST_INVITE_DELAY_SECONDS,
        stop_on_disconnect=True,
    )
    if sent == 0:
        db.audit_log.delete_one({"_id": lock_id})
        raise ValueError(f"No se pudo invitar a ex-participantes ({failed} fallos)")

    db.audit_log.update_one(
        {"_id": lock_id},
        {"$set": {"status": "sent", "sent": sent, "failed": failed, "recipients": len(phones)}},
    )
    return {
        "sent": sent,
        "failed": failed,
        "skipped_no_phone": 0,
        "recipients": len(phones),
    }
