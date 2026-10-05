"""Lógica de rifa: generación de boletos, códigos, asignación."""
from __future__ import annotations

import random
import re
import secrets
import string
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo.database import Database
from pymongo import ReturnDocument

from app import ratelimit

# Alfabeto sin caracteres ambiguos (0/O, 1/I/L)
_ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise ValueError("id inválido")


def digits(value: str | None) -> str:
    """Solo dígitos y, si trae lada/prefijo (+52, 521...), los últimos 10: un mismo
    número siempre se guarda y se compara igual."""
    d = "".join(c for c in (value or "") if c.isdigit())
    return d[-10:] if len(d) > 10 else d


# Estados que ocupan cupo del límite por persona
_STATUS_CUPO = ["delivered", "registered", "scratched", "paid"]


def count_tickets_of_phone(db: Database, rid: ObjectId, phone: str, exclude_folio: int | None = None) -> int:
    q: dict[str, Any] = {"raffle_id": rid, "participant.phone": phone, "status": {"$in": _STATUS_CUPO}}
    if exclude_folio is not None:
        q["folio"] = {"$ne": exclude_folio}
    return db.tickets.count_documents(q)


def audit(db: Database, action: str, *, by: str | None = None, raffle_id: Any = None, folio: int | None = None, **extra: Any) -> None:
    """Bitácora de acciones sensibles (pagos, sorteo, liberaciones, borrados)."""
    try:
        db.audit_log.insert_one({
            "at": now_utc(), "action": action, "by": by,
            "raffle_id": str(raffle_id) if raffle_id else None, "folio": folio, **extra,
        })
    except Exception as exc:  # la bitácora nunca debe tumbar la operación
        print(f"[audit] error: {exc}")


class CodeLocked(Exception):
    """El folio está bloqueado temporalmente por demasiados códigos incorrectos."""


LOCK_AFTER_FAILS = 10
LOCK_MINUTES = 15
MSG_BAD_ACCESS = "Folio o código incorrecto"
MSG_LOCKED = f"Demasiados intentos con este folio. Intenta de nuevo en {LOCK_MINUTES} minutos."


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def verify_code(db: Database, raffle: dict, ticket: dict, code: str) -> bool:
    """Compara el código en tiempo constante. 10 fallos bloquean el folio 15 min y cada
    fallo cuenta contra un presupuesto global del sorteo (no depende de la IP)."""
    locked = _aware(ticket.get("locked_until"))
    if locked and locked > now_utc():
        raise CodeLocked()
    if secrets.compare_digest(ticket["access_code"].upper().encode(), (code or "").strip().upper().encode()):
        if ticket.get("code_fails") or ticket.get("locked_until"):
            db.tickets.update_one({"_id": ticket["_id"]}, {"$set": {"code_fails": 0, "locked_until": None}})
        return True
    doc = db.tickets.find_one_and_update({"_id": ticket["_id"]}, {"$inc": {"code_fails": 1}}, return_document=ReturnDocument.AFTER)
    if doc and doc.get("code_fails", 0) >= LOCK_AFTER_FAILS:
        db.tickets.update_one(
            {"_id": ticket["_id"]},
            {"$set": {"code_fails": 0, "locked_until": now_utc() + timedelta(minutes=LOCK_MINUTES)}},
        )
    ratelimit.fail(f"codefail:{raffle['slug']}")
    return False


def _raffle_of(db: Database, rid: ObjectId) -> dict:
    raffle = db.raffles.find_one({"_id": rid})
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    return raffle


def _ensure_not_drawn(raffle: dict) -> None:
    if raffle.get("status") == "drawing":
        raise ValueError("El sorteo se está realizando; espera unos segundos")
    if raffle.get("status") == "drawn":
        raise ValueError("El sorteo ya fue realizado; no se pueden hacer cambios en los boletos")


def slugify(texto: str) -> str:
    text = unicodedata.normalize("NFKD", texto or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:48] or "sorteo"


CODE_LEN = 6  # 31^6 ≈ 887 millones de combinaciones (4 eran solo ~923 mil)


def generar_codigo(longitud: int = CODE_LEN) -> str:
    return "".join(secrets.choice(_ALFABETO) for _ in range(longitud))


def normalize_meet_url(url: str | None) -> str | None:
    """Normaliza enlace de videollamada: fuerza https y limpia espacios."""
    v = (url or "").strip()
    if not v:
        return None
    if v.lower().startswith("http://"):
        v = "https://" + v[7:]
    elif not v.lower().startswith("https://"):
        v = "https://" + v.lstrip("/")
    return v


def default_meet_url() -> str:
    from app.config import get_settings

    return normalize_meet_url(get_settings().default_meet_url) or ""


def _next_folio_codes(n: int) -> list[str]:
    """n códigos únicos de 4 caracteres."""
    codigos: set[str] = set()
    while len(codigos) < n:
        codigos.add(generar_codigo())
    return list(codigos)


def crear_sorteo(
    db: Database,
    *,
    title: str,
    prize: str,
    prize_value: int,
    price_min: int,
    price_max: int,
    draw_date: str | None = None,
    notes: str | None = None,
    meet_url: str | None = None,
    max_tickets_per_person: int = 3,
) -> dict:
    """Crea el sorteo y genera boletos únicos de price_min..price_max."""
    if price_max < price_min:
        raise ValueError("price_max debe ser >= price_min")

    n = price_max - price_min + 1
    if n > 200:
        raise ValueError("El rango de precios no puede exceder 200 boletos")

    base_slug = slugify(title)
    slug = base_slug
    i = 2
    while db.raffles.find_one({"slug": slug}):
        slug = f"{base_slug}-{i}"
        i += 1

    montos = list(range(price_min, price_max + 1))
    random.shuffle(montos)  # BARAJAR montos para que folio no = precio ordenado
    codigos = _next_folio_codes(n)

    doc = {
        "slug": slug,
        "title": title.strip(),
        "prize": prize.strip(),
        "image_url": None,
        "prize_value": int(prize_value),
        "price_min": int(price_min),
        "price_max": int(price_max),
        "ticket_count": n,
        "status": "open",
        "draw_date": draw_date,
        "notes": notes,
        "meet_url": normalize_meet_url(meet_url) or default_meet_url() or None,
        "max_tickets_per_person": max_tickets_per_person,
        "created_at": now_utc(),
        "drawn_at": None,
        "winner": None,
    }
    result = db.raffles.insert_one(doc)
    raffle_id = result.inserted_id

    tickets = []
    for folio, (monto, cod) in enumerate(zip(montos, codigos), start=1):
        tickets.append(
            {
                "raffle_id": raffle_id,
                "folio": folio,
                "amount": int(monto),
                "access_code": cod,
                "status": "free",
                "participant": None,
                "delivered_at": None,
                "registered_at": None,
                "scratched_at": None,
                "paid_at": None,
                "created_at": now_utc(),
                "updated_at": now_utc(),
            }
        )
    if tickets:
        db.tickets.insert_many(tickets)

    doc["id"] = str(raffle_id)
    return doc


def get_raffle(db: Database, raffle_id: str) -> dict | None:
    doc = db.raffles.find_one({"_id": oid(raffle_id)})
    return _serialize_raffle(doc) if doc else None


def get_raffle_by_slug(db: Database, slug: str) -> dict | None:
    doc = db.raffles.find_one({"slug": slug})
    return _serialize_raffle(doc) if doc else None


def list_raffles(db: Database) -> list[dict]:
    cursor = db.raffles.find().sort("created_at", -1)
    return [_serialize_raffle(d) for d in cursor]


def _serialize_raffle(doc: dict) -> dict:
    out = dict(doc)
    out["id"] = str(doc["_id"])
    out.pop("_id", None)
    return out


def stats_sorteo(db: Database, raffle_id: str) -> dict:
    rid = oid(raffle_id)
    counts = {s: 0 for s in ("free", "delivered", "registered", "scratched", "paid", "released")}
    revenue_expected = 0
    revenue_confirmed = 0
    phones: set[str] = set()

    for t in db.tickets.find({"raffle_id": rid}):
        st = t.get("status", "free")
        counts[st] = counts.get(st, 0) + 1
        amt = t.get("amount") or 0
        if st in ("delivered", "registered", "scratched", "paid"):
            revenue_expected += amt
        if st == "paid":
            revenue_confirmed += amt
        p = t.get("participant") or {}
        if p.get("phone"):
            phones.add(p["phone"])

    total = sum(counts.values())
    return {
        "raffle_id": raffle_id,
        "total_tickets": total,
        **counts,
        "revenue_expected": revenue_expected,
        "revenue_confirmed": revenue_confirmed,
        "participants": len(phones),
    }


# ---------- Boletos ----------
def list_tickets(db: Database, raffle_id: str, status: str | None = None) -> list[dict]:
    q: dict[str, Any] = {"raffle_id": oid(raffle_id)}
    if status:
        q["status"] = status
    return [_serialize_ticket(t) for t in db.tickets.find(q).sort("folio", 1)]


def _serialize_ticket(doc: dict) -> dict:
    out = dict(doc)
    out["id"] = str(doc["_id"])
    out.pop("_id", None)
    out["raffle_id"] = str(doc["raffle_id"])
    return out


def entregar_boleto(
    db: Database,
    raffle_id: str,
    folio: int,
    name: str | None = None,
    phone: str | None = None,
) -> dict | None:
    """Marca el boleto como entregado (y opcionalmente precarga participante)."""
    rid = oid(raffle_id)
    raffle = _raffle_of(db, rid)
    _ensure_not_drawn(raffle)
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] == "paid":
        raise ValueError("El boleto ya está pagado y no se puede modificar")

    phone_d = digits(phone)
    current = ticket.get("participant") or {}
    # No pisar a otra persona que ya tiene el boleto: primero hay que liberarlo
    if current.get("phone") and phone_d and current["phone"] != phone_d:
        raise ValueError("Este boleto ya pertenece a otra persona; libéralo primero")
    if (name or phone) and not (name and phone_d):
        raise ValueError("Para entregar a alguien se necesitan nombre y teléfono")

    if phone_d:
        max_pp = raffle.get("max_tickets_per_person", 3)
        if max_pp and max_pp > 0 and count_tickets_of_phone(db, rid, phone_d, folio) >= max_pp:
            raise ValueError(f"Ese teléfono ya tiene el máximo de {max_pp} boletos")

    update: dict[str, Any] = {"updated_at": now_utc()}
    # No degradar el estado de un boleto ya registrado/raspado
    if ticket["status"] in ("free", "delivered"):
        update["status"] = "delivered"
        update["delivered_at"] = ticket.get("delivered_at") or now_utc()
    if name and phone_d:
        update["participant"] = {"name": name.strip(), "phone": phone_d}

    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"], "status": {"$ne": "paid"}},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise ValueError("El boleto cambió mientras se procesaba; intenta de nuevo")
    return _serialize_ticket(doc)


def marcar_pagado(
    db: Database, raffle_id: str, folio: int, note: str | None = None, by: str | None = None
) -> dict | None:
    rid = oid(raffle_id)
    _ensure_not_drawn(_raffle_of(db, rid))
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] not in ("delivered", "registered", "scratched"):
        raise ValueError(f"No se puede marcar como pagado desde el estado '{ticket['status']}'")
    p = ticket.get("participant") or {}
    if not p.get("name") or not p.get("phone"):
        raise ValueError("El boleto no tiene participante (nombre y teléfono); regístralo antes de cobrar")

    now = now_utc()
    doc = db.tickets.find_one_and_update(
        # el filtro de estado evita pisar un cambio concurrente (p. ej. auto-liberado)
        {"_id": ticket["_id"], "status": {"$in": ["delivered", "registered", "scratched"]}},
        {"$set": {"status": "paid", "paid_at": now, "updated_at": now, "payment_note": note, "paid_by": by}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise ValueError("El boleto cambió mientras se procesaba; revisa su estado")
    # Si el sorteo arrancó justo entre la validación y la escritura, deshacer: un pago
    # fuera de la lista de elegibles no debe quedar registrado.
    after = db.raffles.find_one({"_id": rid}, {"status": 1, "draw_audit": 1}) or {}
    elegibles = (after.get("draw_audit") or {}).get("eligible_folios", [])
    if after.get("status") == "drawing" or (after.get("status") == "drawn" and folio not in elegibles):
        db.tickets.update_one(
            {"_id": ticket["_id"], "status": "paid"},
            {"$set": {"status": ticket["status"], "paid_at": None, "paid_by": None, "updated_at": now_utc()}},
        )
        raise ValueError("El sorteo se estaba realizando; el pago no se registró")
    audit(db, "pay", by=by, raffle_id=rid, folio=folio, amount=ticket.get("amount"), note=note)
    return _serialize_ticket(doc)


def desmarcar_pago(db: Database, raffle_id: str, folio: int, by: str | None = None, reason: str | None = None) -> dict | None:
    """Deshace un 'pagado' marcado por error (solo antes del sorteo). Vuelve a 'registered'/'delivered'."""
    rid = oid(raffle_id)
    _ensure_not_drawn(_raffle_of(db, rid))
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] != "paid":
        raise ValueError("El boleto no está marcado como pagado")
    back = "scratched" if ticket.get("scratched_at") else ("registered" if ticket.get("registered_at") else "delivered")
    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"], "status": "paid"},
        {"$set": {"status": back, "paid_at": None, "paid_by": None, "updated_at": now_utc()}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise ValueError("El boleto cambió mientras se procesaba; revisa su estado")
    after = db.raffles.find_one({"_id": rid}, {"status": 1, "draw_audit": 1}) or {}
    if after.get("status") == "drawn" and folio in (after.get("draw_audit") or {}).get("eligible_folios", []):
        db.tickets.update_one(
            {"_id": ticket["_id"], "status": back},
            {"$set": {"status": "paid", "paid_at": ticket.get("paid_at"), "paid_by": ticket.get("paid_by"), "updated_at": now_utc()}},
        )
        raise ValueError("El sorteo ya se realizó; el pago no se puede deshacer")
    audit(db, "unpay", by=by, raffle_id=rid, folio=folio, reason=reason)
    return _serialize_ticket(doc)


def liberar_boleto(db: Database, raffle_id: str, folio: int, by: str | None = None) -> dict | None:
    rid = oid(raffle_id)
    _ensure_not_drawn(_raffle_of(db, rid))
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] == "paid":
        raise ValueError("No se puede liberar un boleto pagado")

    # Una sola operación atómica: libera y rota el código
    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"], "status": {"$ne": "paid"}},
        {
            "$set": {
                "status": "free",
                "participant": None,
                "delivered_at": None,
                "registered_at": None,
                "scratched_at": None,
                "access_code": generar_codigo(),
                "updated_at": now_utc(),
            },
            "$inc": {"code_rotation": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise ValueError("El boleto se pagó mientras se procesaba; no se liberó")
    audit(db, "release", by=by, raffle_id=rid, folio=folio, previous=ticket.get("participant"), previous_status=ticket["status"])
    return _serialize_ticket(doc)


# ---------- Flujo público ----------
def find_ticket_by_access(db: Database, raffle_slug: str, folio: int, code: str) -> tuple[dict | None, dict | None]:
    raffle = db.raffles.find_one({"slug": raffle_slug})
    if not raffle:
        return None, None
    ticket = db.tickets.find_one({"raffle_id": raffle["_id"], "folio": folio})
    return raffle, ticket


def check_access(db: Database, raffle_slug: str, folio: int, code: str) -> dict:
    raffle, ticket = find_ticket_by_access(db, raffle_slug, folio, code)
    if not raffle:
        return {"ok": False, "message": "Sorteo no encontrado"}
    if not ticket:
        ratelimit.fail(f"codefail:{raffle['slug']}")
        return {"ok": False, "message": MSG_BAD_ACCESS}  # igual que código malo: no revela qué folios existen
    try:
        if not verify_code(db, raffle, ticket, code):
            return {"ok": False, "message": MSG_BAD_ACCESS}
    except CodeLocked:
        return {"ok": False, "message": MSG_LOCKED}

    status = ticket["status"]
    needs_reg = status in ("free", "delivered")
    if status == "free":
        # Se permite auto-registro si tiene el código
        pass

    return {
        "ok": True,
        "raffle_title": raffle["title"],
        "raffle_slug": raffle["slug"],
        "folio": folio,
        "status": status,
        "needs_registration": needs_reg and not (ticket.get("participant") or {}).get("name"),
        "participant_name": (ticket.get("participant") or {}).get("name"),
        "amount": ticket["amount"] if status in ("scratched", "paid") else None,
        "message": "Acceso concedido",
    }


def register_participant(
    db: Database,
    raffle_slug: str,
    folio: int,
    code: str,
    name: str,
    phone: str,
) -> dict:
    raffle, ticket = find_ticket_by_access(db, raffle_slug, folio, code)
    if not raffle or not ticket:
        return {"ok": False, "message": MSG_BAD_ACCESS}
    try:
        if not verify_code(db, raffle, ticket, code):
            return {"ok": False, "message": MSG_BAD_ACCESS}
    except CodeLocked:
        return {"ok": False, "message": MSG_LOCKED}
    if ticket["status"] == "paid":
        return {"ok": False, "message": "Este folio ya está pagado"}
    if ticket["status"] == "released":
        return {"ok": False, "message": "Este folio fue liberado"}

    if raffle.get("status") == "drawn":
        return {"ok": False, "message": "El sorteo ya fue realizado"}
    if raffle.get("status") != "open" and ticket["status"] == "free":
        return {"ok": False, "message": "La venta de este sorteo ya está cerrada"}

    phone_d = digits(phone)
    # Múltiples boletos permitidos (hasta max_tickets_per_person; 0 = sin límite)
    max_pp = raffle.get("max_tickets_per_person", 3)
    if max_pp and max_pp > 0 and count_tickets_of_phone(db, raffle["_id"], phone_d, folio) >= max_pp:
        return {"ok": False, "message": f"Ese teléfono ya tiene el máximo de {max_pp} boletos"}
    # Un boleto que ya tiene dueño no puede ser retomado por otro teléfono
    owner = (ticket.get("participant") or {}).get("phone")
    if owner and owner != phone_d:
        return {"ok": False, "message": "Este folio ya está registrado con otro teléfono"}

    new_status = "registered" if ticket["status"] in ("free", "delivered") else ticket["status"]
    res = db.tickets.update_one(
        {"_id": ticket["_id"], "status": {"$in": ["free", "delivered", "registered", "scratched"]}},
        {
            "$set": {
                "participant": {"name": name.strip(), "phone": phone_d},
                "status": new_status,
                "registered_at": ticket.get("registered_at") or now_utc(),
                "updated_at": now_utc(),
            }
        },
    )
    if res.matched_count == 0:
        return {"ok": False, "message": "Este folio cambió de estado; intenta de nuevo"}
    return {
        "ok": True,
        "folio": folio,
        "amount": ticket["amount"] if ticket["status"] in ("scratched", "paid") else None,
        "message": "Registro exitoso",
    }


def scratch_ticket(db: Database, raffle_slug: str, folio: int, code: str) -> dict:
    raffle, ticket = find_ticket_by_access(db, raffle_slug, folio, code)
    if not raffle or not ticket:
        return {"ok": False, "message": MSG_BAD_ACCESS}
    try:
        if not verify_code(db, raffle, ticket, code):
            return {"ok": False, "message": MSG_BAD_ACCESS}
    except CodeLocked:
        return {"ok": False, "message": MSG_LOCKED}

    status = ticket["status"]
    if raffle.get("status") == "drawn" and status != "paid" and status != "scratched":
        return {"ok": False, "message": "El sorteo ya fue realizado"}
    if status == "paid":
        return {"ok": True, "amount": ticket["amount"], "folio": folio, "message": "Ya está pagado"}
    if status == "scratched":
        return {"ok": True, "amount": ticket["amount"], "folio": folio, "message": "Ya estaba raspado"}
    if status in ("free", "delivered", "registered"):
        if not (ticket.get("participant") or {}).get("name"):
            return {"ok": False, "message": "Debes registrarte antes de raspar"}
        res = db.tickets.update_one(
            {"_id": ticket["_id"], "status": {"$in": ["free", "delivered", "registered"]}},
            {
                "$set": {
                    "status": "scratched",
                    "scratched_at": now_utc(),
                    "updated_at": now_utc(),
                }
            },
        )
        if res.matched_count == 0:
            return {"ok": False, "message": "Este folio cambió de estado; intenta de nuevo"}
        return {"ok": True, "amount": ticket["amount"], "folio": folio, "message": "¡Boleto raspado!"}
    return {"ok": False, "message": "Estado no válido para raspar"}


def reportar_pago(db: Database, raffle_slug: str, folio: int, code: str) -> dict:
    """La participante avisa 'ya pagué': el boleto deja de ser candidato a auto-liberación."""
    raffle, ticket = find_ticket_by_access(db, raffle_slug, folio, code)
    if not raffle or not ticket:
        return {"ok": False, "message": MSG_BAD_ACCESS}
    try:
        if not verify_code(db, raffle, ticket, code):
            return {"ok": False, "message": MSG_BAD_ACCESS}
    except CodeLocked:
        return {"ok": False, "message": MSG_LOCKED}
    if ticket["status"] == "paid":
        return {"ok": True, "message": "Tu pago ya está confirmado"}
    if ticket["status"] not in ("registered", "scratched"):
        return {"ok": False, "message": "Este boleto no puede reportar pago"}
    now = now_utc()
    db.tickets.update_one(
        {"_id": ticket["_id"], "status": {"$in": ["registered", "scratched"]}},
        {"$set": {"payment_reported_at": ticket.get("payment_reported_at") or now, "updated_at": now}},
    )
    audit(db, "payment_reported", raffle_id=raffle["_id"], folio=folio)
    return {"ok": True, "message": "Listo, avisamos a la organizadora. Confirmará tu pago pronto."}


# ---------- Sorteo ----------
def run_draw(db: Database, raffle_id: str, by: str | None = None) -> dict:
    """Sorteo entre boletos pagados.

    1) Candado atómico: el sorteo pasa a 'drawing' (solo uno puede lograrlo) y mientras tanto
       nadie puede marcar/deshacer pagos. 2) Se elige entre los pagados y se comprueba que el
       elegido siga pagado. 3) Se sella como 'drawn'. Cualquier fallo libera el candado.
    """
    rid = oid(raffle_id)
    raffle = _raffle_of(db, rid)
    prev = raffle.get("status")
    if prev == "drawn":
        raise ValueError("El sorteo ya fue realizado")
    if prev == "drawing":
        raise ValueError("El sorteo ya se está realizando")
    if prev == "draft":
        raise ValueError("El sorteo está en borrador")

    lock = db.raffles.update_one(
        {"_id": rid, "status": {"$in": ["open", "closed"]}},
        {"$set": {"status": "drawing", "prev_status": prev, "drawing_at": now_utc()}},
    )
    if lock.modified_count == 0:
        raise ValueError("El sorteo ya fue realizado o se está realizando")

    try:
        ganador = pagados = None
        for _ in range(3):
            pagados = list(db.tickets.find({"raffle_id": rid, "status": "paid"}))
            if not pagados:
                raise ValueError("No hay boletos pagados para sortear")
            sin_dueno = [t["folio"] for t in pagados if not (t.get("participant") or {}).get("phone")]
            if sin_dueno:
                raise ValueError(f"Hay boletos pagados sin participante (folios {sin_dueno}); corrígelos antes de sortear")
            candidato = secrets.choice(pagados)
            # el elegido debe seguir pagado en este instante
            ok = db.tickets.update_one(
                {"_id": candidato["_id"], "status": "paid"},
                {"$set": {"is_winner": True, "updated_at": now_utc()}},
            )
            if ok.matched_count == 1:
                ganador = candidato
                break
        if ganador is None:
            raise ValueError("Los pagos cambiaron durante el sorteo; intenta de nuevo")

        now = now_utc()
        winner_doc = {
            "ticket_id": str(ganador["_id"]),
            "folio": ganador["folio"],
            "amount": ganador["amount"],
            "participant": ganador.get("participant"),
        }
        fin = db.raffles.update_one(
            {"_id": rid, "status": "drawing"},
            {
                "$set": {
                    "status": "drawn",
                    "drawn_at": now,
                    "winner": winner_doc,
                    # evidencia para poder verificar el resultado después
                    "draw_audit": {
                        "eligible_folios": sorted(t["folio"] for t in pagados),
                        "total_paid": len(pagados),
                        "method": "secrets.choice (CSPRNG)",
                        "by": by,
                    },
                }
            },
        )
        if fin.modified_count == 0:
            raise ValueError("El sorteo cambió de estado durante la ejecución; intenta de nuevo")
    except Exception:
        db.raffles.update_one({"_id": rid, "status": "drawing"}, {"$set": {"status": prev}})
        db.tickets.update_many({"raffle_id": rid, "is_winner": True}, {"$set": {"is_winner": False}})
        raise
    audit(db, "draw", by=by, raffle_id=rid, folio=ganador["folio"], total_paid=len(pagados))
    return {"winner": winner_doc, "total_paid": len(pagados)}


def close_raffle(db: Database, raffle_id: str, by: str | None = None) -> dict:
    rid = oid(raffle_id)
    raffle = _raffle_of(db, rid)
    if raffle["status"] in ("drawn", "drawing"):
        raise ValueError("El sorteo ya fue realizado o se está realizando")
    db.raffles.update_one({"_id": rid, "status": {"$nin": ["drawn", "drawing"]}}, {"$set": {"status": "closed"}})
    audit(db, "close", by=by, raffle_id=rid)
    return get_raffle(db, raffle_id)  # type: ignore[return-value]
