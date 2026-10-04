"""Rate limit en memoria, por IP real del cliente (ventana de 60 s)."""
from __future__ import annotations

import time

from fastapi import HTTPException, Request

from app.config import get_settings

_hits: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    """IP del cliente detrás de proxies (Vercel -> Render).

    El proxy más cercano agrega la IP que vio al final de X-Forwarded-For; con
    `proxy_hops` proxies, la IP real queda en esa posición contando desde el final.
    Entradas más a la izquierda las puede falsear el cliente, por eso no se usan.
    """
    xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if xff:
        hops = max(1, get_settings().proxy_hops)
        return xff[max(0, len(xff) - hops)]
    return request.client.host if request.client else "unknown"


def check_rate(request: Request, bucket: str, max_per_min: int) -> None:
    key = f"{bucket}:{client_ip(request)}"
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < 60]
    if len(recent) >= max_per_min:
        _hits[key] = recent
        raise HTTPException(429, "Demasiados intentos. Espera un minuto.")
    recent.append(now)
    _hits[key] = recent
    # limpieza ocasional para que el dict no crezca sin límite
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] >= 60]:
            _hits.pop(k, None)
