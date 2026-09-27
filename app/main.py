"""FastAPI entrypoint — sorteo-natura API."""
from __future__ import annotations

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
        seed_admin(get_db())
    except Exception as exc:
        print(f"[startup] MongoDB no disponible: {exc}")
    yield


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

    @app.get("/health")
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
