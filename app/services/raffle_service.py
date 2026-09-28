"""Lógica de rifa: generación de boletos, códigos, asignación."""
from __future__ import annotations

import random
import re
import secrets
import string
import unicodedata
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo.database import Database
from pymongo import ReturnDocument

# Alfabeto sin caracteres ambiguos (0/O, 1/I/L)
_ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise ValueError("id inválido")


def slugify(texto: str) -> str:
    text = unicodedata.normalize("NFKD", texto or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:48] or "sorteo"


def generar_codigo(longitud: int = 4) -> str:
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
        codigos.add(generar_codigo(4))
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
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] == "paid":
        raise ValueError("El boleto ya está pagado y no se puede modificar")

    # Múltiples boletos permitidos (hasta max_tickets_per_person)
    if phone:
        phone_d = "".join(c for c in phone if c.isdigit())
        raffle_doc = db.raffles.find_one({"_id": rid})
        max_pp = (raffle_doc or {}).get("max_tickets_per_person", 3)
        if max_pp > 0:
            count = db.tickets.count_documents({
                "raffle_id": rid,
                "participant.phone": phone_d,
                "status": {"$in": ["delivered", "registered", "scratched", "paid"]},
                "folio": {"$ne": folio},
            })
            if count >= max_pp:
                raise ValueError(f"Ese teléfono ya tiene el máximo de {max_pp} boletos")

    update: dict[str, Any] = {
        "status": "delivered",
        "updated_at": now_utc(),
        "delivered_at": ticket.get("delivered_at") or now_utc(),
    }
    if name and phone:
        update["participant"] = {
            "name": name.strip(),
            "phone": "".join(c for c in phone if c.isdigit()),
        }
    elif ticket.get("participant") is None and (name or phone):
        update["participant"] = {"name": (name or "").strip() or None, "phone": phone}

    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"]},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_ticket(doc)


def marcar_pagado(db: Database, raffle_id: str, folio: int, note: str | None = None) -> dict | None:
    rid = oid(raffle_id)
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] not in ("delivered", "registered", "scratched"):
        raise ValueError(f"No se puede marcar como pagado desde el estado '{ticket['status']}'")

    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"]},
        {
            "$set": {
                "status": "paid",
                "paid_at": now_utc(),
                "updated_at": now_utc(),
                "payment_note": note,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_ticket(doc)


def liberar_boleto(db: Database, raffle_id: str, folio: int) -> dict | None:
    rid = oid(raffle_id)
    ticket = db.tickets.find_one({"raffle_id": rid, "folio": folio})
    if not ticket:
        return None
    if ticket["status"] == "paid":
        raise ValueError("No se puede liberar un boleto pagado")
    if ticket["status"] == "scratched":
        raise ValueError("No se puede liberar un boleto ya raspado — el precio ya fue revelado")

    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"]},
        {
            "$set": {
                "status": "free",
                "participant": None,
                "delivered_at": None,
                "registered_at": None,
                "scratched_at": None,
                "updated_at": now_utc(),
            },
            "$inc": {"code_rotation": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    # Rotar código al liberar
    nuevo_cod = generar_codigo(4)
    doc = db.tickets.find_one_and_update(
        {"_id": ticket["_id"]},
        {"$set": {"access_code": nuevo_cod, "updated_at": now_utc()}},
        return_document=ReturnDocument.AFTER,
    )
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
        return {"ok": False, "message": "Folio no encontrado"}
    if ticket["access_code"].upper() != code.strip().upper():
        return {"ok": False, "message": "Código de acceso incorrecto"}

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
        return {"ok": False, "message": "Folio o sorteo no encontrado"}
    if ticket["access_code"].upper() != code.strip().upper():
        return {"ok": False, "message": "Código de acceso incorrecto"}
    if ticket["status"] == "paid":
        return {"ok": False, "message": "Este folio ya está pagado"}
    if ticket["status"] == "released":
        return {"ok": False, "message": "Este folio fue liberado"}

    phone_d = "".join(c for c in phone if c.isdigit())
    # Múltiples boletos permitidos (hasta max_tickets_per_person)
    max_pp = raffle.get("max_tickets_per_person", 3)
    if max_pp > 0:
        count = db.tickets.count_documents({
            "raffle_id": raffle["_id"],
            "participant.phone": phone_d,
            "status": {"$in": ["registered", "scratched", "paid"]},
            "folio": {"$ne": folio},
        })
        if count >= max_pp:
            return {"ok": False, "message": f"Ese teléfono ya tiene el máximo de {max_pp} boletos"}

    new_status = "registered" if ticket["status"] in ("free", "delivered") else ticket["status"]
    db.tickets.update_one(
        {"_id": ticket["_id"]},
        {
            "$set": {
                "participant": {"name": name.strip(), "phone": phone_d},
                "status": new_status,
                "registered_at": ticket.get("registered_at") or now_utc(),
                "updated_at": now_utc(),
            }
        },
    )
    return {
        "ok": True,
        "folio": folio,
        "amount": ticket["amount"] if ticket["status"] in ("scratched", "paid") else None,
        "message": "Registro exitoso",
    }


def scratch_ticket(db: Database, raffle_slug: str, folio: int, code: str) -> dict:
    raffle, ticket = find_ticket_by_access(db, raffle_slug, folio, code)
    if not raffle or not ticket:
        return {"ok": False, "message": "Folio o sorteo no encontrado"}
    if ticket["access_code"].upper() != code.strip().upper():
        return {"ok": False, "message": "Código de acceso incorrecto"}

    status = ticket["status"]
    if status == "paid":
        return {"ok": True, "amount": ticket["amount"], "folio": folio, "message": "Ya está pagado"}
    if status == "scratched":
        return {"ok": True, "amount": ticket["amount"], "folio": folio, "message": "Ya estaba raspado"}
    if status in ("free", "delivered", "registered"):
        if not (ticket.get("participant") or {}).get("name"):
            return {"ok": False, "message": "Debes registrarte antes de raspar"}
        db.tickets.update_one(
            {"_id": ticket["_id"]},
            {
                "$set": {
                    "status": "scratched",
                    "scratched_at": now_utc(),
                    "updated_at": now_utc(),
                }
            },
        )
        return {"ok": True, "amount": ticket["amount"], "folio": folio, "message": "¡Boleto raspado!"}
    return {"ok": False, "message": "Estado no válido para raspar"}


# ---------- Sorteo ----------
def run_draw(db: Database, raffle_id: str) -> dict:
    """Sorteo entre boletos pagados. Devuelve el ganador."""
    rid = oid(raffle_id)
    raffle = db.raffles.find_one({"_id": rid})
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    if raffle.get("status") == "drawn":
        raise ValueError("El sorteo ya fue realizado")
    if raffle.get("status") == "draft":
        raise ValueError("El sorteo está en borrador")

    pagados = list(db.tickets.find({"raffle_id": rid, "status": "paid"}))
    if not pagados:
        raise ValueError("No hay boletos pagados para sortear")

    ganador = secrets.choice(pagados)
    winner_doc = {
        "ticket_id": str(ganador["_id"]),
        "folio": ganador["folio"],
        "amount": ganador["amount"],
        "participant": ganador.get("participant"),
    }

    db.raffles.update_one(
        {"_id": rid},
        {
            "$set": {
                "status": "drawn",
                "drawn_at": now_utc(),
                "winner": winner_doc,
            }
        },
    )
    db.tickets.update_one({"_id": ganador["_id"]}, {"$set": {"is_winner": True, "updated_at": now_utc()}})
    return {"winner": winner_doc, "total_paid": len(pagados)}


def close_raffle(db: Database, raffle_id: str) -> dict:
    rid = oid(raffle_id)
    raffle = db.raffles.find_one({"_id": rid})
    if not raffle:
        raise ValueError("Sorteo no encontrado")
    if raffle["status"] == "drawn":
        raise ValueError("El sorteo ya fue realizado")
    db.raffles.update_one({"_id": rid}, {"$set": {"status": "closed"}})
    return get_raffle(db, raffle_id)  # type: ignore[return-value]
