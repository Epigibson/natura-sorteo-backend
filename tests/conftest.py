import os

# Antes de importar la app: configuración de prueba (nunca la de producción)
os.environ["JWT_SECRET"] = "test-secret-test-secret-test-secret-123"
os.environ["SEED_ADMIN_PHONE"] = "5500000000"
os.environ["SEED_ADMIN_PASSWORD"] = "TestPassword123!"
os.environ["MONGO_URL"] = "mongodb://localhost:1/never-used"

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.db as dbmod
import app.ratelimit as rl
from app.main import app, seed_admin


@pytest.fixture()
def db():
    dbmod._client = mongomock.MongoClient()
    d = dbmod.get_db()
    dbmod.ensure_indexes()
    seed_admin(d)
    rl._hits.clear()
    yield d
    dbmod._client = None


@pytest.fixture()
def client(db):
    return TestClient(app)


@pytest.fixture()
def admin(client):
    r = client.post("/api/v1/auth/login", json={"phone": "5500000000", "password": "TestPassword123!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def raffle(client, admin):
    r = client.post("/api/v1/raffles", headers=admin, json={
        "title": "Rifa Prueba", "prize": "Perfume", "prize_value": 500,
        "price_min": 30, "price_max": 39, "max_tickets_per_person": 2,
    })
    assert r.status_code == 201, r.text
    return r.json()
