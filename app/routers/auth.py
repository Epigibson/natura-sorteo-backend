"""Auth: login y refresh para el dashboard de Yuri."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.db import get_db
from app.ratelimit import check_rate, fail, guard
from app.schemas import LoginIn, RefreshIn, TokenOut
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request) -> TokenOut:
    check_rate(request, "login", 8)
    # Tope de fallos por teléfono, independiente de la IP (que se puede falsear llamando
    # directo al backend): 20 fallos en 15 min. Alto a propósito para que un tercero no
    # pueda dejar a Yuri fuera con unos pocos intentos.
    guard(f"loginfail:{body.phone}", 20, window=900, message="Demasiados intentos. Espera 15 minutos.")
    db = get_db()
    user = db.users.find_one({"phone": body.phone})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        fail(f"loginfail:{body.phone}", window=900)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Teléfono o contraseña incorrectos")
    if not user.get("active", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cuenta desactivada")

    role = user.get("role", "collab")
    name = user.get("name", "")
    sub = str(user["_id"])
    return TokenOut(
        access_token=create_access_token(sub=sub, role=role, name=name, tv=user.get("token_version", 0)),
        refresh_token=create_refresh_token(sub=sub, role=role, tv=user.get("token_version", 0)),
        role=role,
        name=name,
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request) -> TokenOut:
    check_rate(request, "refresh", 30)
    payload = decode_token(body.refresh_token, expect="refresh")
    db = get_db()
    from bson import ObjectId

    try:
        user = db.users.find_one({"_id": ObjectId(payload["sub"])})
    except Exception:
        user = None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario no encontrado")
    if not user.get("active", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cuenta desactivada")
    if user.get("token_version", 0) != payload.get("tv", 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión cerrada; inicia sesión de nuevo")
    role = user.get("role", "collab")
    name = user.get("name", "")
    sub = str(user["_id"])
    return TokenOut(
        access_token=create_access_token(sub=sub, role=role, name=name, tv=user.get("token_version", 0)),
        refresh_token=create_refresh_token(sub=sub, role=role, tv=user.get("token_version", 0)),
        role=role,
        name=name,
    )
