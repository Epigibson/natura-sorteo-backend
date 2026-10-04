from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.main import run_maintenance
from app.services import raffle_service as rs

PHONE = "5511223344"


def claim(client, slug, folio, phone=PHONE, name="Ana Pérez"):
    return client.post(f"/api/v1/public/raffles/{slug}/claim", json={"folio": folio, "name": name, "phone": phone})


def pay(client, admin, rid, folio):
    return client.post(f"/api/v1/raffles/{rid}/tickets/{folio}/pay", headers=admin, json={})


# ---------- reclamo ----------
def test_claim_ok_and_taken(client, raffle):
    r = claim(client, raffle["slug"], 1)
    assert r.status_code == 200 and r.json()["code"]
    assert claim(client, raffle["slug"], 1, phone="5599887766").status_code == 400  # ya tomado


def test_claim_respects_limit_and_zero_means_unlimited(client, admin, db, raffle):
    s = raffle["slug"]
    assert claim(client, s, 1).status_code == 200
    assert claim(client, s, 2).status_code == 200
    assert claim(client, s, 3).status_code == 400  # límite 2
    db.raffles.update_one({"slug": s}, {"$set": {"max_tickets_per_person": 0}})
    assert claim(client, s, 3).status_code == 200  # 0 = sin límite (antes bloqueaba a todos)


def test_claim_closed_raffle(client, admin, raffle):
    client.post(f"/api/v1/raffles/{raffle['id']}/close", headers=admin)
    assert claim(client, raffle["slug"], 1).status_code == 400


def test_claim_unpaid_per_ip_cap(client, db, raffle, monkeypatch):
    import app.routers.public as pub
    monkeypatch.setattr(pub, "MAX_UNPAID_PER_IP", 2)
    db.raffles.update_one({"slug": raffle["slug"]}, {"$set": {"max_tickets_per_person": 0}})
    assert claim(client, raffle["slug"], 1, phone="5500000001").status_code == 200
    assert claim(client, raffle["slug"], 2, phone="5500000002").status_code == 200
    assert claim(client, raffle["slug"], 3, phone="5500000003").status_code == 429


def test_rate_limit_is_per_ip_not_global(client, raffle):
    s = raffle["slug"]
    for i in range(10):
        client.post(f"/api/v1/public/raffles/{s}/claim", json={"folio": 99, "name": "Ana", "phone": PHONE},
                    headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"})
    blocked = client.post(f"/api/v1/public/raffles/{s}/claim", json={"folio": 99, "name": "Ana", "phone": PHONE},
                          headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"})
    other = client.post(f"/api/v1/public/raffles/{s}/claim", json={"folio": 99, "name": "Ana", "phone": PHONE},
                        headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"})
    assert blocked.status_code == 429
    assert other.status_code != 429  # otra IP no se ve afectada


def test_login_rate_limit(client):
    for _ in range(8):
        client.post("/api/v1/auth/login", json={"phone": "5500000000", "password": "mala-mala"})
    assert client.post("/api/v1/auth/login", json={"phone": "5500000000", "password": "mala-mala"}).status_code == 429


# ---------- liberar ----------
def test_public_release_needs_code(client, raffle):
    s = raffle["slug"]
    code = claim(client, s, 1).json()["code"]
    url = f"/api/v1/public/raffles/{s}/release"
    assert client.post(url, json={"folio": 1, "phone": PHONE, "code": "ZZZZ"}).status_code == 403
    assert client.post(url, json={"folio": 1, "phone": "5500000099", "code": code}).status_code == 403
    assert client.post(url, json={"folio": 1, "phone": PHONE, "code": code}).status_code == 200
    assert claim(client, s, 1, phone="5599887766").status_code == 200  # volvió a estar libre


def test_public_release_blocked_after_scratch(client, raffle):
    s = raffle["slug"]
    code = claim(client, s, 1).json()["code"]
    assert client.post("/api/v1/public/scratch", json={"folio": 1, "code": code, "raffle_slug": s}).status_code == 200
    r = client.post(f"/api/v1/public/raffles/{s}/release", json={"folio": 1, "phone": PHONE, "code": code})
    assert r.status_code == 400


# ---------- pagos ----------
def test_cannot_pay_ticket_without_participant(client, admin, raffle):
    client.post(f"/api/v1/raffles/{raffle['id']}/tickets/1/assign", headers=admin, json={})  # entregado sin dueño
    assert pay(client, admin, raffle["id"], 1).status_code == 400


def test_pay_unpay_and_audit(client, admin, db, raffle):
    claim(client, raffle["slug"], 1)
    assert pay(client, admin, raffle["id"], 1).status_code == 200
    r = client.post(f"/api/v1/raffles/{raffle['id']}/tickets/1/unpay", headers=admin, json={"reason": "error"})
    assert r.status_code == 200 and r.json()["status"] == "registered"
    assert {a["action"] for a in db.audit_log.find()} >= {"pay", "unpay"}


def test_assign_does_not_overwrite_other_person_or_downgrade(client, admin, raffle):
    claim(client, raffle["slug"], 1)
    base = f"/api/v1/raffles/{raffle['id']}/tickets/1/assign"
    r = client.post(base, headers=admin, json={"name": "Otra Persona", "phone": "5500000077"})
    assert r.status_code == 400
    r = client.post(base, headers=admin, json={})
    assert r.json()["status"] == "registered"  # no se degrada a 'delivered'


# ---------- sorteo ----------
def test_draw_only_paid_and_only_once(client, admin, raffle):
    rid = raffle["id"]
    assert client.post(f"/api/v1/raffles/{rid}/draw", headers=admin).status_code == 400  # nadie pagó
    claim(client, raffle["slug"], 1)
    claim(client, raffle["slug"], 2, phone="5599887766")
    pay(client, admin, rid, 1)
    r = client.post(f"/api/v1/raffles/{rid}/draw", headers=admin)
    assert r.status_code == 200 and r.json()["winner"]["folio"] == 1
    assert client.post(f"/api/v1/raffles/{rid}/draw", headers=admin).status_code == 400
    info = client.get(f"/api/v1/raffles/{rid}", headers=admin).json()
    assert info["draw_audit"]["eligible_folios"] == [1]


def test_draw_race_has_single_winner(db, admin, client, raffle):
    """Si otro proceso sortea entre la lectura y la escritura, el segundo falla."""
    rid = raffle["id"]
    claim(client, raffle["slug"], 1)
    pay(client, admin, rid, 1)
    real = rs._raffle_of
    def stale(db_, oid_):
        doc = real(db_, oid_)
        db_.raffles.update_one({"_id": oid_}, {"$set": {"status": "drawn", "winner": {"folio": 7}}})  # el "otro" gana
        return doc
    rs._raffle_of = stale
    try:
        with pytest.raises(ValueError):
            rs.run_draw(db, rid)
    finally:
        rs._raffle_of = real
    assert db.raffles.find_one({"_id": ObjectId(rid)})["winner"]["folio"] == 7  # no fue pisado


def test_no_changes_after_draw(client, admin, raffle):
    rid = raffle["id"]
    claim(client, raffle["slug"], 1)
    claim(client, raffle["slug"], 2, phone="5599887766")
    pay(client, admin, rid, 1)
    client.post(f"/api/v1/raffles/{rid}/draw", headers=admin)
    assert pay(client, admin, rid, 2).status_code == 400
    assert client.post(f"/api/v1/raffles/{rid}/tickets/2/release", headers=admin).status_code == 400


def test_delete_blocked_with_payments(client, admin, raffle):
    claim(client, raffle["slug"], 1)
    pay(client, admin, raffle["id"], 1)
    assert client.delete(f"/api/v1/raffles/{raffle['id']}", headers=admin).status_code == 400


# ---------- mantenimiento ----------
def test_auto_release_and_close(db, client, raffle):
    s = raffle["slug"]
    claim(client, s, 1)
    claim(client, s, 2, phone="5599887766")
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    db.tickets.update_many({"folio": {"$in": [1, 2]}}, {"$set": {"registered_at": old}})
    db.tickets.update_one({"folio": 2}, {"$set": {"status": "paid"}})
    run_maintenance(db)
    assert db.tickets.find_one({"folio": 1})["status"] == "free"
    assert db.tickets.find_one({"folio": 2})["status"] == "paid"  # un pago nunca se libera
    assert db.audit_log.count_documents({"action": "auto_release"}) == 1


def test_draw_date_validation_and_local_close(client, admin, db):
    base = {"title": "Rifa X", "prize": "Premio", "prize_value": 1, "price_min": 1, "price_max": 3}
    assert client.post("/api/v1/raffles", headers=admin, json={**base, "draw_date": "10/10/2026"}).status_code == 422
    tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    r = client.post("/api/v1/raffles", headers=admin, json={**base, "draw_date": tomorrow})
    assert r.status_code == 201
    run_maintenance(db)
    assert db.raffles.find_one({"slug": r.json()["slug"]})["status"] == "open"  # aún no vence


# ---------- Excel ----------
def test_excel_formula_is_text(client, admin, raffle):
    from io import BytesIO
    from openpyxl import load_workbook
    claim(client, raffle["slug"], 1, name='=HYPERLINK("http://evil","x")')
    r = client.get(f"/api/v1/raffles/{raffle['id']}/export/participants", headers=admin)
    ws = load_workbook(BytesIO(r.content))["Participantes"]
    cell = next(c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and "HYPERLINK" in c.value)
    assert cell.data_type != "f"


def test_upload_rejects_fake_image(client, admin, monkeypatch):
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "x")
    from app.config import get_settings
    get_settings.cache_clear()
    r = client.post("/api/v1/upload-image", headers=admin, files={"file": ("a.png", b"not an image", "image/png")})
    get_settings.cache_clear()
    assert r.status_code == 400


# ---------- sincronización del navegador del participante ----------
def test_mine_detects_ticket_released_by_staff(client, admin, raffle):
    s = raffle["slug"]
    code = claim(client, s, 1).json()["code"]
    url = f"/api/v1/public/raffles/{s}/mine"
    body = {"tickets": [{"folio": 1, "code": code}]}
    assert client.post(url, json=body).json()["tickets"][0]["valid"] is True
    client.post(f"/api/v1/raffles/{raffle['id']}/tickets/1/release", headers=admin)
    assert client.post(url, json=body).json()["tickets"][0]["valid"] is False  # código rotado
    # y aunque otra persona lo tome, el código viejo no le sirve a la anterior
    claim(client, s, 1, phone="5599887766")
    assert client.post(url, json=body).json()["tickets"][0]["valid"] is False


# ---------- sesiones ----------
def test_password_change_revokes_old_sessions(client, admin):
    old_refresh = client.post("/api/v1/auth/login", json={"phone": "5500000000", "password": "TestPassword123!"}).json()["refresh_token"]
    r = client.post("/api/v1/change-password", headers=admin,
                    json={"current_password": "TestPassword123!", "new_password": "OtraClave456!"})
    assert r.status_code == 200
    assert client.get("/api/v1/me", headers=admin).status_code == 401  # token viejo muerto
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401
    fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/v1/me", headers=fresh).status_code == 200  # el de quien cambió sigue vivo


def test_deactivated_user_loses_access_immediately(client, admin, db):
    db.users.update_one({"phone": "5500000000"}, {"$set": {"active": False}})
    assert client.get("/api/v1/me", headers=admin).status_code == 401
