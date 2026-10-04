"""FastAPI entrypoint — sorteo-natura API."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import ensure_indexes, get_db
from app.routers import auth, public, staff
from app.security import hash_password


def seed_admin(db) -> None:
    """Bootstrap dev: usuario admin de Yuri si no existe."""
    if db.users.find_one({"phone": get_settings().seed_admin_phone}):
        return
    db.users.insert_one(
        {
            "phone": get_settings().seed_admin_phone,
            "name": get_settings().seed_admin_name,
            "password_hash": hash_password(get_settings().seed_admin_password),
            "role": "admin",
            "active": True,
        }
    )
    print(f"[seed] admin creado: {get_settings().seed_admin_phone}")


def _local_today() -> str:
    """Fecha de hoy (YYYY-MM-DD) en la zona horaria del sorteo, no en UTC."""
    from datetime import datetime, timedelta, timezone

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(get_settings().timezone)
    except Exception:
        tz = timezone(timedelta(hours=-6))  # México centro, sin horario de verano
    return datetime.now(tz).strftime("%Y-%m-%d")


def run_maintenance(db) -> None:
    """Cierra sorteos vencidos y libera boletos sin pagar. Es idempotente y atómico por boleto."""
    from datetime import datetime, timedelta, timezone

    from app.services import raffle_service as rs

    now = datetime.now(timezone.utc)
    # Cierra sorteos abiertos cuya fecha ya llegó (según hora local)
    db.raffles.update_many(
        {"status": "open", "draw_date": {"$nin": [None, ""], "$lte": _local_today()}},
        {"$set": {"status": "closed"}},
    )
    # Auto-liberar boletos sin pagar tras N horas, solo en sorteos aún no sorteados
    hours = get_settings().auto_release_hours
    if hours <= 0:
        return
    cutoff = now - timedelta(hours=hours)
    active = [r["_id"] for r in db.raffles.find({"status": {"$in": ["open", "closed"]}}, {"_id": 1})]
    for t in db.tickets.find({
        "raffle_id": {"$in": active},
        "status": {"$in": ["registered", "scratched"]},
        "registered_at": {"$lt": cutoff},
    }):
        res = db.tickets.update_one(
            # el filtro de estado evita liberar un boleto que se pagó justo ahora
            {"_id": t["_id"], "status": {"$in": ["registered", "scratched"]}},
            {"$set": {
                "status": "free",
                "participant": None,
                "delivered_at": None,
                "registered_at": None,
                "scratched_at": None,
                "claimed_from": None,
                "access_code": rs.generar_codigo(4),
                "updated_at": now,
            }},
        )
        if res.modified_count:
            rs.audit(db, "auto_release", raffle_id=t["raffle_id"], folio=t["folio"],
                     previous=t.get("participant"), previous_status=t["status"])
            print(f"[auto-release] folio {t['folio']} liberado (sin pagar)")


async def _auto_close_loop():
    while True:
        try:
            await asyncio.to_thread(run_maintenance, get_db())
        except Exception as exc:
            print(f"[auto-close] error: {exc}")
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
        seed_admin(get_db())
    except Exception as exc:
        print(f"[startup] MongoDB no disponible: {exc}")
    task = asyncio.create_task(_auto_close_loop())
    yield
    task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Sorteo Natura API",
        description="Backend de rifas con raspadito digital — dashboard para Yuri",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(staff.router)
    app.include_router(public.router)

    @app.api_route("/health", methods=["GET", "HEAD"])
    def health() -> dict:
        from app.db import get_client

        try:
            get_client().admin.command("ping")
            mongo = "ok"
        except Exception:
            mongo = "down"
        return {"status": "ok", "mongo": mongo}

    return app


app = create_app()
