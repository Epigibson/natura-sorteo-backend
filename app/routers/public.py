"""Rutas públicas del participante (sin auth) — tablero, acceso, registro, raspar."""
from __future__ import annotations

import random

from fastapi import APIRouter, HTTPException, status

from app.db import get_db
from app.schemas import (
    AccessCheckIn,
    AccessCheckOut,
    PublicRaffleOut,
    RegisterIn,
    ScratchIn,
    ScratchOut,
)
from app.services import raffle_service as rs

router = APIRouter(prefix="/api/v1/public", tags=["public"])


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
                "has_name": bool(p.get("name")),
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
def access(body: AccessCheckIn) -> AccessCheckOut:
    result = rs.check_access(get_db(), body.raffle_slug, body.folio, body.code)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, result.get("message", "Acceso denegado"))
    return AccessCheckOut(**result)


@router.post("/register")
def register(body: RegisterIn) -> dict:
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
def scratch(body: ScratchIn) -> ScratchOut:
    result = rs.scratch_ticket(get_db(), body.raffle_slug, body.folio, body.code)
    if not result.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.get("message", "No se puede raspar"))
    return ScratchOut(**result)
