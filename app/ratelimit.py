"""Límites de intentos en memoria (ventana deslizante) por IP y por clave arbitraria.

Nota: es por proceso. Con una sola instancia (Render free) basta; con varias, el límite
efectivo se multiplica, por eso las defensas críticas (bloqueo por folio, presupuesto de
fallos por sorteo) también viven en la base de datos o son globales por sorteo.
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request

from app.config import get_settings

_hits: dict[str, deque] = {}
_MAX_WINDOW = 3600.0
_MAX_KEYS = 20000
_last_sweep = 0.0


def client_ip(request: Request) -> str:
    """IP del cliente detrás de proxies (Vercel -> Render).

    El proxy más cercano agrega la IP que vio al final de X-Forwarded-For; con
    `proxy_hops` proxies, la IP real queda en esa posición contando desde el final.
    OJO: si alguien llama directo al backend (sin pasar por Vercel) puede falsear esa
    posición, por eso la IP nunca es la única defensa (ver guard()/fail() por sorteo).
    """
    xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if xff:
        hops = max(1, get_settings().proxy_hops)
        return xff[max(0, len(xff) - hops)]
    return request.client.host if request.client else "unknown"


def _sweep(now: float) -> None:
    """Limpieza periódica (no en cada llamada) para que el dict no crezca sin límite."""
    global _last_sweep
    if now - _last_sweep < 60 and len(_hits) < _MAX_KEYS:
        return
    _last_sweep = now
    for k in [k for k, q in _hits.items() if not q or now - q[-1] > _MAX_WINDOW]:
        _hits.pop(k, None)
    if len(_hits) > _MAX_KEYS:  # ataque con claves únicas: mejor perder historial que memoria
        _hits.clear()


def _recent(key: str, window: float, now: float) -> deque:
    q = _hits.setdefault(key, deque())
    while q and now - q[0] >= window:
        q.popleft()
    return q


def check_rate(request: Request, bucket: str, max_per_min: int, cost: int = 1) -> None:
    """Cuenta `cost` intentos de esta IP en el bucket; 429 si excede el máximo por minuto."""
    now = time.time()
    _sweep(now)
    q = _recent(f"{bucket}:{client_ip(request)}", 60, now)
    if len(q) + cost > max_per_min:
        raise HTTPException(429, "Demasiados intentos. Espera un minuto.")
    q.extend([now] * cost)


def guard(key: str, limit: int, window: float = 60, message: str | None = None) -> None:
    """429 si ya se registraron `limit` fallos con esta clave dentro de la ventana."""
    now = time.time()
    if len(_recent(key, window, now)) >= limit:
        raise HTTPException(429, message or "Demasiados intentos fallidos. Espera unos minutos.")


def fail(key: str, window: float = 60) -> None:
    """Registra un fallo (código/contraseña incorrectos) para `guard`."""
    now = time.time()
    _sweep(now)
    _recent(key, window, now).append(now)
