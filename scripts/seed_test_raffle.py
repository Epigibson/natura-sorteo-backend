"""Siembra (o re-siembra) un sorteo de prueba listo para transmitir.

Uso (desde backend/):
    python scripts/seed_test_raffle.py

Borra cualquier sorteo cuyo título empiece con "[TEST]" y crea uno nuevo con
participantes falsos: ~25 boletos pagados, unos cuantos por cobrar y el resto
libres. Así el botón "🎲 Sortear premio" siempre tiene entre quién sortear y la
prueba se puede repetir cuantas veces quieras.
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BACKEND_ROOT)  # Settings lee .env relativo al cwd
sys.path.insert(0, BACKEND_ROOT)

from app.services import raffle_service as rs  # noqa: E402
from app.db import ensure_indexes, get_db  # noqa: E402

TITLE = "[TEST] Sorteo de Transmisión"

NOMBRES = [
    "María Fernanda López", "Carlos Ramírez", "Ana Sofía Hernández", "Luis Miguel Torres",
    "Guadalupe Martínez", "Jorge Alberto Ruiz", "Fernanda Gómez", "Ricardo Salazar",
    "Valeria Cruz", "Miguel Ángel Navarro", "Daniela Romero", "Alejandro Vargas",
    "Patricia Mendoza", "Roberto Delgado", "Ximena Aguilar", "Héctor Villalobos",
    "Gabriela Ortega", "Francisco Javier Ríos", "Mariana Estrada", "Óscar Fuentes",
    "Lucía Campos", "Emiliano Zúñiga", "Regina Peña", "Raúl Ibarra",
    "Adriana Morales", "Santiago Robles", "Paulina Guerrero", "Iván Cortés",
]

# Algunos teléfonos repetidos a propósito (multi-boleto, máximo 3 por persona)
TELEFONOS_FIJOS = ["5512340001", "5512340002", "5512340003"]


def fecha_sorteo() -> str:
    tz = None
    try:
        from zoneinfo import ZoneInfo

        from app.config import get_settings

        tz = ZoneInfo(get_settings().timezone)
    except Exception:
        pass
    manana = datetime.now(tz) + timedelta(days=1)
    return manana.strftime("%Y-%m-%d")


def borrar_tests(db) -> None:
    viejos = list(db.raffles.find({"title": {"$regex": r"^\[TEST\]"}}, {"_id": 1, "title": 1}))
    for r in viejos:
        db.tickets.delete_many({"raffle_id": r["_id"]})
        db.raffles.delete_one({"_id": r["_id"]})
        print(f"  ✕ borrado: {r['title']} ({r['_id']})")


def main() -> None:
    db = get_db()
    ensure_indexes()
    print("→ Limpiando sorteos [TEST] anteriores…")
    borrar_tests(db)

    print("→ Creando sorteo de prueba…")
    raffle = rs.crear_sorteo(
        db,
        title=TITLE,
        prize="iPhone 15 Pro Max 📱",
        prize_value=28000,
        price_min=100,
        price_max=139,  # 40 boletos
        draw_date=fecha_sorteo(),
        notes="Sorteo de prueba para pulir la transmisión en vivo. No es un sorteo real.",
        max_tickets_per_person=3,
    )
    rid = raffle["id"]
    tickets = rs.list_tickets(db, rid)
    random.shuffle(tickets)

    tel_pool = TELEFONOS_FIJOS + [f"55{random.randint(10000000, 99999999)}" for _ in range(25)]
    random.shuffle(tel_pool)

    # ~25 pagados (algunas personas con 2 boletos), 6 entregados por cobrar, resto libres
    idx = 0
    pagados = 0
    for i, t in enumerate(tickets[:25]):
        nombre = NOMBRES[idx % len(NOMBRES)]
        tel = tel_pool[idx % len(tel_pool)]
        idx += 1
        rs.entregar_boleto(db, rid, t["folio"], name=nombre, phone=tel)
        rs.marcar_pagado(db, rid, t["folio"], note="prueba", by="seed-test")
        pagados += 1

    for t in tickets[25:31]:
        nombre = NOMBRES[idx % len(NOMBRES)]
        tel = tel_pool[idx % len(tel_pool)]
        idx += 1
        rs.entregar_boleto(db, rid, t["folio"], name=nombre, phone=tel)

    stats = rs.stats_sorteo(db, rid)
    slug = raffle["slug"]
    print()
    print("✅ Sorteo de prueba listo:")
    print(f"   Título   : {TITLE}")
    print(f"   id       : {rid}")
    print(f"   slug     : {slug}")
    print(f"   Pagados  : {pagados} de {stats['total_tickets']} boletos")
    print(f"   Por cobrar: {stats.get('delivered', 0)}")
    print()
    print(f"   Dashboard : /sorteos/{rid}")
    print(f"   Landing   : /sorteo/{slug}")
    print(f"   Tablero   : /sorteo/{slug}/tablero")


if __name__ == "__main__":
    main()
