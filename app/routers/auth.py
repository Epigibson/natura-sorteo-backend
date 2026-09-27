"""Auth: login y refresh para el dashboard de Yuri."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.db import get_db
from app.schemas import LoginIn, RefreshIn, TokenOut
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn) -> TokenOut:
    db = get_db()
    user = db.users.find_one({"phone": body.phone})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Teléfono o contraseña incorrectos")
    if not user.get("active", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cuenta desactivada")

    role = user.get("role", "collab")
    name = user.get("name", "")
    sub = str(user["_id"])
    return TokenOut(
        access_token=create_access_token(sub=sub, role=role, name=name),
        refresh_token=create_refresh_token(sub=sub, role=role),
        role=role,
        name=name,
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn) -> TokenOut:
    payload = decode_token(body.refresh_token, expect="refresh")
    db = get_db()
    from bson import ObjectId

    try:
        user = db.users.find_one({"_id": ObjectId(payload["sub"])})
    except Exception:
        user = None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario no encontrado")
    role = user.get("role", "collab")
    name = user.get("name", "")
    sub = str(user["_id"])
    return TokenOut(
        access_token=create_access_token(sub=sub, role=role, name=name),
        refresh_token=create_refresh_token(sub=sub, role=role),
        role=role,
        name=name,
    )
