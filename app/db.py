"""MongoDB connection helpers (pymongo sync, sin ODM)."""
from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database

from app.config import get_settings

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(get_settings().mongo_url, serverSelectionTimeoutMS=4000)
    return _client


def get_db() -> Database:
    return get_client()[get_settings().mongo_db]


def ensure_indexes() -> None:
    db = get_db()
    db.users.create_index([("phone", ASCENDING)], unique=True)
    db.raffles.create_index([("slug", ASCENDING)], unique=True)
    db.raffles.create_index([("created_at", DESCENDING)])
    db.tickets.create_index([("raffle_id", ASCENDING), ("folio", ASCENDING)], unique=True)
    db.tickets.create_index([("raffle_id", ASCENDING), ("status", ASCENDING)])
    db.tickets.create_index([("raffle_id", ASCENDING), ("participant.phone", ASCENDING)])
    db.audit_log.create_index([("raffle_id", ASCENDING), ("at", DESCENDING)])
