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
    print(f"[seed] admin creado: {get_settings().seed_admin_phone} / {get_settings().seed_admin_password}")


def _new_code():
    import secrets
    return "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(4))


async def _auto_close_loop():
    """Cierra sorteos automáticamente cuando llega la fecha del sorteo."""
    while True:
        try:
            from datetime import datetime, timedelta, timezone
            db = get_db()
            now = datetime.now(timezone.utc)
            # Cerrar sorteos abiertos cuya fecha ya pasó
            db.raffles.update_many(
                {"status": "open", "draw_date": {"$ne": None, "$lte": now.strftime("%Y-%m-%d")}},
                {"$set": {"status": "closed"}},
            )
            # Auto-liberar boletos raspados sin pagar después de 24h
            cutoff = now - timedelta(hours=24)
            expired = db.tickets.find({
                "status": {"$in": ["registered", "scratched"]},
                "registered_at": {"$lt": cutoff},
            })
            for t in expired:
                db.tickets.update_one(
                    {"_id": t["_id"]},
                    {"$set": {
                        "status": "free",
                        "participant": None,
                        "delivered_at": None,
                        "registered_at": None,
                        "scratched_at": None,
                        "access_code": _new_code(),
                        "updated_at": now,
                    }},
                )
                print(f"[auto-release] folio {t['folio']} liberado (sin pagar)")
        except Exception as exc:
            print(f"[auto-close] error: {exc}")
        await asyncio.sleep(3600)


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
