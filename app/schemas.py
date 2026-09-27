"""Pydantic schemas — contratos de la API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

TicketStatus = Literal["free", "delivered", "registered", "scratched", "paid", "released"]
RaffleStatus = Literal["draft", "open", "closed", "drawn"]


# ---------- Auth ----------
class LoginIn(BaseModel):
    phone: str = Field(min_length=8, max_length=15)
    password: str = Field(min_length=4)

    @field_validator("phone")
    @classmethod
    def _digits(cls, v: str) -> str:
        d = "".join(c for c in v if c.isdigit())
        if len(d) < 8:
            raise ValueError("Teléfono inválido")
        return d


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    name: str


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=4)
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("La nueva contraseña debe tener al menos 8 caracteres")
        return v


# ---------- Raffle ----------
class RaffleCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    prize: str = Field(min_length=3, max_length=300)
    prize_value: int = Field(ge=0, description="Valor del premio en MXN")
    price_min: int = Field(ge=1, description="Monto mínimo del boleto (MXN)")
    price_max: int = Field(ge=1, description="Monto máximo del boleto (MXN)")
    draw_date: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("price_max")
    @classmethod
    def _max_ge_min(cls, v: int, info) -> int:
        if "price_min" in info.data and v < info.data["price_min"]:
            raise ValueError("price_max debe ser >= price_min")
        return v


class RaffleOut(BaseModel):
    id: str
    slug: str
    title: str
    prize: str
    prize_value: int
    price_min: int
    price_max: int
    ticket_count: int
    status: RaffleStatus
    draw_date: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    drawn_at: Optional[datetime] = None
    winner: Optional[dict] = None


class RaffleStats(BaseModel):
    raffle_id: str
    total_tickets: int
    free: int
    delivered: int
    registered: int
    scratched: int
    paid: int
    released: int
    revenue_expected: int
    revenue_confirmed: int
    participants: int


# ---------- Ticket ----------
class TicketOut(BaseModel):
    id: str
    raffle_id: str
    folio: int
    amount: Optional[int] = None  # None si aún no se revela al staff público
    status: TicketStatus
    access_code: str
    participant: Optional[dict] = None
    delivered_at: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    scratched_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    updated_at: datetime


class TicketAssign(BaseModel):
    """Entregar folio + código a una persona (queda 'delivered')."""
    name: Optional[str] = Field(default=None, min_length=3, max_length=120)
    phone: Optional[str] = Field(default=None, min_length=10, max_length=15)


class TicketPaidIn(BaseModel):
    note: Optional[str] = None


# ---------- Público (participante) ----------
class AccessCheckIn(BaseModel):
    folio: int = Field(ge=1)
    code: str = Field(min_length=3, max_length=12)
    raffle_slug: str = Field(min_length=3)


class AccessCheckOut(BaseModel):
    ok: bool
    raffle_title: str
    raffle_slug: str
    folio: int
    status: TicketStatus
    needs_registration: bool
    participant_name: Optional[str] = None
    amount: Optional[int] = None  # solo si ya raspó
    message: str


class RegisterIn(BaseModel):
    folio: int = Field(ge=1)
    code: str = Field(min_length=3, max_length=12)
    raffle_slug: str = Field(min_length=3)
    name: str = Field(min_length=3, max_length=120)
    phone: str = Field(min_length=10, max_length=15)

    @field_validator("phone")
    @classmethod
    def _digits(cls, v: str) -> str:
        d = "".join(c for c in v if c.isdigit())
        if len(d) < 10:
            raise ValueError("Teléfono debe tener al menos 10 dígitos")
        return d


class ScratchIn(BaseModel):
    folio: int = Field(ge=1)
    code: str = Field(min_length=3, max_length=12)
    raffle_slug: str = Field(min_length=3)


class ScratchOut(BaseModel):
    ok: bool
    amount: int
    folio: int
    message: str


class PublicRaffleOut(BaseModel):
    slug: str
    title: str
    prize: str
    prize_value: int
    price_min: int
    price_max: int
    ticket_count: int
    status: RaffleStatus
    draw_date: Optional[str] = None
    paid_count: int
    drawn: bool
    winner_folio: Optional[int] = None
    winner_name: Optional[str] = None
