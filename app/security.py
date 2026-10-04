"""Passwords (argon2id), JWT, y dependencias de auth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_hasher = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=2)
_bearer = HTTPBearer(auto_error=False)

ROLE_ADMIN = "admin"
ROLE_COLLAB = "collab"  # asistente de Yuri: ve y asigna, no cierra sorteos


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_access_token(*, sub: str, role: str, name: str, tv: int = 0) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "role": role,
        "name": name,
        "type": "access",
        "tv": tv,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_ttl_min),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


def create_refresh_token(*, sub: str, role: str, tv: int = 0) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "role": role,
        "type": "refresh",
        "tv": tv,
        "iat": now,
        "exp": now + timedelta(days=s.refresh_ttl_days),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


def decode_token(token: str, *, expect: str) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_alg])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")
    if payload.get("type") != expect:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tipo de token incorrecto")
    return payload


class CurrentUser(dict):
    """Dict con sub, role, name."""


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta token")
    payload = decode_token(creds.credentials, expect="access")
    # Validar contra la BD: usuario existente, activo y con sesión vigente
    # (al cambiar la contraseña sube token_version y las sesiones viejas dejan de servir).
    from bson import ObjectId

    from app.db import get_db

    try:
        user = get_db().users.find_one({"_id": ObjectId(payload["sub"])})
    except Exception:
        user = None
    if not user or not user.get("active", True) or user.get("token_version", 0) != payload.get("tv", 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión inválida o cerrada")
    # El rol sale de la BD, no del token: un cambio de rol aplica de inmediato
    return CurrentUser(sub=payload["sub"], role=user.get("role", ROLE_COLLAB), name=user.get("name", ""))


def require_admin(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Se requiere rol admin")
    return user


def require_staff(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if user.get("role") not in (ROLE_ADMIN, ROLE_COLLAB):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso restringido")
    return user
