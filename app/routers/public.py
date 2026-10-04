"""Rutas públicas del participante (sin auth) — tablero, acceso, registro, raspar."""
from __future__ import annotations

import random
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from app.db import get_db
from app.schemas import (
    AccessCheckIn,
    AccessCheckOut,
    ClaimIn,
    MineIn,
    PublicRaffleOut,
    RegisterIn,
    ReleaseIn,
    ScratchIn,
    ScratchOut,
)
from app.ratelimit import check_rate, client_ip
from app.services import raffle_service as rs

router = APIRouter(prefix="/api/v1/public", tags=["public"])
# Máximo de boletos sin pagar que puede tener tomados una misma IP por sorteo
MAX_UNPAID_PER_IP = 6


@router.get("/raffles/{slug}", response_model=PublicRaffleOut)
def public_raffle(slug: str) -> dict:
    db = get_db()
    raffle = db.raffles.find_one({"slug": slug})
    if not raffle:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sorteo no encontrado")
    paid = db.tickets.count_documents({"raffle_id": raffle["_id"], "status": "paid"})
    drawn = raffle.get("status") == "drawn"
    winner = raffle.get("winner") or {}
    return {
        "slug": raffle["slug"],
        "title": raffle["title"],
        "prize": raffle["prize"],
        "prize_value": raffle["prize_value"],
        "image_url": raffle.get("image_url"),
        "meet_url": raffle.get("meet_url") or rs.default_meet_url() or None,
        "price_min": raffle["price_min"],
        "price_max": raffle["price_max"],
        "ticket_count": raffle["ticket_count"],
        "status": raffle["status"],
        "draw_date": raffle.get("draw_date"),
        "paid_count": paid,
        "drawn": drawn,
        "winner_folio": winner.get("folio") if drawn else None,
        "winner_name": (winner.get("participant") or {}).get("name") if drawn else None,
    }


@router.get("/raffles/{slug}/board")
def public_board(slug: str) -> dict:
    """Tablero público: lista de folios con estado, SIN montos, orden aleatorio."""
    db = get_db()
    raffle = db.raffles.find_one({"slug": slug})
    if not raffle:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sorteo no encontrado")

    tickets = list(db.tickets.find({"raffle_id": raffle["_id"]}))
    cards = []
    for t in tickets:
        p = t.get("participant") or {}
        cards.append(
            {
                "folio": t["folio"],
                "status": t.get("status", "free"),
                
            }
        )

    # Barajar para que nadie pueda adivinar qué folio tiene qué monto
    random.shuffle(cards)

    paid = sum(1 for c in cards if c["status"] == "paid")
    free = sum(1 for c in cards if c["status"] == "free")

    return {
        "slug": raffle["slug"],
        "title": raffle["title"],
        "prize": raffle["prize"],
        "price_min": raffle["price_min"],
        "price_max": raffle["price_max"],
        "ticket_count": raffle["ticket_count"],
        "status": raffle["status"],
        "drawn": raffle.get("status") == "drawn",
        "winner_folio": (raffle.get("winner") or {}).get("folio"),
        "paid_count": paid,
        "free_count": free,
        "cards": cards,
    }


@router.post("/access", response_model=AccessCheckOut)
def access(body: AccessCheckIn, request: Request) -> AccessCheckOut:
    check_rate(request, "access", 20)
    result = rs.check_access(get_db(), body.raffle_slug, body.folio, body.code)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, result.get("message", "Acceso denegado"))
    return AccessCheckOut(**result)


@router.post("/register")
def register(body: RegisterIn, request: Request) -> dict:
    check_rate(request, "register", 10)
    result = rs.register_participant(
        get_db(),
        body.raffle_slug,
        body.folio,
        body.code,
        body.name,
        body.phone,
    )
    if not result.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("message", "Error de registro"))
    return result


@router.post("/scratch", response_model=ScratchOut)
def scratch(body: ScratchIn, request: Request) -> ScratchOut:
    check_rate(request, "scratch", 20)
    result = rs.scratch_ticket(get_db(), body.raffle_slug, body.folio, body.code)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("message", "No se puede raspar"))
    return ScratchOut(**result)


@router.post("/raffles/{slug}/claim")
def claim_ticket(slug: str, body: ClaimIn, request: Request) -> dict:
    """Reclamar un boleto libre: registra nombre+teléfono y asigna el folio. No requiere código."""
    check_rate(request, "claim", 10)
    ip = client_ip(request)
    name = body.name.strip()
    phone = body.phone

    db = get_db()
    raffle = db.raffles.find_one({"slug": slug})
    if not raffle:
        raise HTTPException(404, "Sorteo no encontrado")
    if raffle.get("status") != "open":
        raise HTTPException(400, "El sorteo no está abierto")

    ticket = db.tickets.find_one({"raffle_id": raffle["_id"], "folio": body.folio})
    if not ticket:
        raise HTTPException(404, "Folio no encontrado")
    if ticket["status"] != "free":
        raise HTTPException(400, "Este boleto ya fue tomado")

    # Límite de boletos por persona (0 = sin límite)
    max_pp = raffle.get("max_tickets_per_person", 3)
    if max_pp and max_pp > 0 and rs.count_tickets_of_phone(db, raffle["_id"], phone) >= max_pp:
        raise HTTPException(400, f"Este teléfono ya tiene el máximo de {max_pp} boletos")

    # Antiacaparamiento: una misma IP no puede mantener tomados demasiados boletos sin pagar
    unpaid_from_ip = db.tickets.count_documents({
        "raffle_id": raffle["_id"],
        "claimed_from": ip,
        "status": {"$in": ["registered", "scratched"]},
    })
    if unpaid_from_ip >= MAX_UNPAID_PER_IP:
        raise HTTPException(429, "Tienes demasiados boletos pendientes de pago. Paga alguno o contacta a Yuri.")

    # Reclamo atómico: solo si sigue libre (evita condiciones de carrera)
    now = datetime.now(timezone.utc)
    result = db.tickets.update_one(
        {"_id": ticket["_id"], "status": "free"},
        {"$set": {
            "status": "registered",
            "participant": {"name": name, "phone": phone},
            "registered_at": now,
            "delivered_at": now,
            "updated_at": now,
            "claimed_from": ip,
        }},
    )
    if result.modified_count == 0:
        raise HTTPException(400, "Este boleto fue tomado por otra persona")

    return {
        "ok": True,
        "folio": body.folio,
        "code": ticket["access_code"],
        "message": f"Folio {body.folio} asignado a {name}",
        "notify": True,
    }


@router.post("/raffles/{slug}/release")
def release_ticket(slug: str, body: ReleaseIn, request: Request) -> dict:
    """Liberar un boleto reclamado (solo si no ha sido raspado ni pagado).

    Requiere folio + teléfono + código de acceso: el teléfono solo no basta."""
    check_rate(request, "release", 10)
    phone = body.phone and "".join(ch for ch in body.phone if ch.isdigit())

    db = get_db()
    raffle = db.raffles.find_one({"slug": slug})
    if not raffle:
        raise HTTPException(404, "Sorteo no encontrado")
    if raffle.get("status") == "drawn":
        raise HTTPException(400, "El sorteo ya fue realizado")

    ticket = db.tickets.find_one({"raffle_id": raffle["_id"], "folio": body.folio})
    if not ticket:
        raise HTTPException(404, "Folio no encontrado")

    p = ticket.get("participant") or {}
    if p.get("phone") != phone or ticket["access_code"].upper() != body.code.strip().upper():
        # mismo mensaje para ambos casos: no revelar cuál dato falló
        raise HTTPException(403, "Este boleto no te pertenece")

    now = datetime.now(timezone.utc)
    res = db.tickets.update_one(
        # el filtro de estado evita liberar un boleto que se acaba de raspar o pagar
        {"_id": ticket["_id"], "status": {"$in": ["delivered", "registered"]}},
        {"$set": {
            "status": "free",
            "participant": None,
            "delivered_at": None,
            "registered_at": None,
            "claimed_from": None,
            "access_code": rs.generar_codigo(4),
            "updated_at": now,
        }},
    )
    if res.modified_count == 0:
        raise HTTPException(400, "No se puede liberar un boleto ya raspado o pagado")
    rs.audit(db, "release_by_participant", raffle_id=raffle["_id"], folio=body.folio)
    return {"ok": True, "folio": body.folio, "message": "Boleto liberado"}


@router.post("/raffles/{slug}/mine")
def check_mine(slug: str, body: MineIn, request: Request) -> dict:
    """El navegador del participante pregunta si sus boletos guardados siguen siendo suyos.

    Si Yuri (o la auto-liberación) soltó un boleto, su código rotó y aquí sale como
    'released', para que el navegador lo borre de su almacenamiento local."""
    check_rate(request, "mine", 30)
    db = get_db()
    raffle = db.raffles.find_one({"slug": slug})
    if not raffle:
        raise HTTPException(404, "Sorteo no encontrado")
    out = []
    for item in body.tickets:
        t = db.tickets.find_one({"raffle_id": raffle["_id"], "folio": item.folio})
        valid = bool(
            t
            and t["access_code"].upper() == item.code.strip().upper()
            and t["status"] in ("delivered", "registered", "scratched", "paid")
        )
        out.append({"folio": item.folio, "valid": valid, "status": t["status"] if valid else "released"})
    return {"tickets": out}
