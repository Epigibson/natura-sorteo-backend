"""FastAPI entrypoint — sorteo-natura API."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    from app.services.raffle_service import local_today

    return local_today()


def run_maintenance(db) -> None:
    """Cierra sorteos vencidos y libera boletos sin pagar. Es idempotente y atómico por boleto."""
    from datetime import datetime, timedelta, timezone

    from app.services import raffle_service as rs

    now = datetime.now(timezone.utc)
    # Cierra la venta al TERMINAR el día del sorteo (hora local): ese día se sigue vendiendo
    # y, en cuanto se sortea, el sorteo queda sellado de todos modos.
    db.raffles.update_many(
        {"status": "open", "draw_date": {"$nin": [None, ""], "$lt": _local_today()}},
        {"$set": {"status": "closed"}},
    )
    # Un sorteo que quedó en 'drawing' (el proceso murió a media ejecución) se destraba
    for r in db.raffles.find({"status": "drawing", "drawing_at": {"$lt": now - timedelta(minutes=10)}}):
        db.raffles.update_one({"_id": r["_id"], "status": "drawing"}, {"$set": {"status": r.get("prev_status") or "closed"}})
        rs.audit(db, "draw_unlock", raffle_id=r["_id"])

    # Auto-liberar boletos sin pagar tras N horas. Solo en sorteos ABIERTOS: una vez cerrada la
    # venta nadie podría volver a registrarse, y perder un boleto ya pagado sería irreparable.
    hours = get_settings().auto_release_hours
    if hours <= 0:
        return
    cutoff = now - timedelta(hours=hours)
    active = [r["_id"] for r in db.raffles.find({"status": "open"}, {"_id": 1})]
    for t in db.tickets.find({
        "raffle_id": {"$in": active},
        "status": {"$in": ["registered", "scratched"]},
        "registered_at": {"$lt": cutoff},
        "payment_reported_at": None,  # quien avisó "ya pagué" espera confirmación: no se libera
    }):
        res = db.tickets.update_one(
            # el filtro de estado evita liberar un boleto que se pagó justo ahora
            {"_id": t["_id"], "status": {"$in": ["registered", "scratched"]},
             "registered_at": t["registered_at"], "payment_reported_at": None},
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


_indexes_ready = False
_last_maintenance = 0.0


def _ensure_setup(db) -> None:
    """Índices + admin semilla. Si Mongo no respondió al arrancar, se reintenta después."""
    global _indexes_ready
    if _indexes_ready:
        return
    ensure_indexes()
    seed_admin(db)
    _indexes_ready = True


def maybe_maintenance() -> None:
    """Mantenimiento 'perezoso': en Render free el proceso duerme y el loop no corre, así que
    también se ejecuta al recibir tráfico público (máx. una vez por minuto)."""
    import time

    global _last_maintenance
    now = time.time()
    if now - _last_maintenance < 60:
        return
    _last_maintenance = now
    try:
        db = get_db()
        _ensure_setup(db)
        run_maintenance(db)
    except Exception as exc:
        print(f"[maintenance] error: {exc}")


async def _auto_close_loop():
    while True:
        await asyncio.to_thread(maybe_maintenance)
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        _ensure_setup(get_db())
    except Exception as exc:
        print(f"[startup] MongoDB no disponible, se reintentará: {exc}")
    task = asyncio.create_task(_auto_close_loop())
    yield
    task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    docs = settings.enable_docs
    app = FastAPI(
        title="Sorteo Natura API",
        description="Backend de rifas con raspadito digital — dashboard para Yuri",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=False,  # se usa token Bearer, no cookies
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _guard_and_headers(request: Request, call_next):
        # Tope de tamaño: 1 MB para JSON, 6 MB solo para subir imágenes
        limit = 6 * 1024 * 1024 if request.url.path.endswith("/upload-image") else 1024 * 1024
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if size > limit:
            return JSONResponse({"detail": "Petición demasiado grande"}, status_code=413)
        if request.url.path.startswith("/api/v1/public/"):
            await asyncio.to_thread(maybe_maintenance)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
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
