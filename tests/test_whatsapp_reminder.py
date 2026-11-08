from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services import whatsapp_reminder as wr
from app.services.raffle_service import oid


def _settings(url="http://evo:8080", key="k", instance="sorteo"):
    return type("S", (), {
        "evolution_url": url,
        "evolution_api_key": key,
        "evolution_instance": instance,
    })()


def test_build_reminder_message_includes_title_and_board(db, raffle):
    msg = wr.build_reminder_message(
        {"title": "Rifa Prueba", "prize": "Perfume", "slug": raffle["slug"], "draw_date": "2026-12-01"},
        "https://ejemplo.test",
    )
    assert "Rifa Prueba" in msg
    assert "Perfume" in msg
    assert "2026-12-01" in msg
    assert f"https://ejemplo.test/sorteo/{raffle['slug']}/tablero" in msg


def test_build_reminder_message_draw_date_fallback(db, raffle):
    msg = wr.build_reminder_message(
        {"title": "Rifa", "prize": "X", "slug": raffle["slug"], "draw_date": None},
        "https://ejemplo.test/",
    )
    assert "por definir" in msg
    assert "https://ejemplo.test/sorteo/" in msg


def test_collect_unique_phones_skips_missing_and_dedupes(client, admin, db, raffle):
    rid = raffle["id"]
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin,
                json={"name": "Ana", "phone": "5511223344"})
    client.post(f"/api/v1/raffles/{rid}/tickets/2/assign", headers=admin,
                json={"name": "Ana2", "phone": "5511223344"})
    phones = wr.collect_unique_phones(db, rid)
    assert phones == ["5511223344"]


def test_collect_unique_phones_sorted_unique(client, admin, db, raffle):
    rid = raffle["id"]
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin,
                json={"name": "Ana", "phone": "5599887766"})
    client.post(f"/api/v1/raffles/{rid}/tickets/2/assign", headers=admin,
                json={"name": "Beto", "phone": "5511223344"})
    phones = wr.collect_unique_phones(db, rid)
    assert phones == ["5511223344", "5599887766"]


def test_wa_jid():
    assert wr.wa_jid("5511223344") == "525511223344"


def test_send_mass_remind_requires_config(db, raffle, monkeypatch):
    monkeypatch.setattr(wr, "get_settings", lambda: _settings(url=""))
    with pytest.raises(ValueError, match="configurado"):
        wr.send_mass_remind(db, raffle["id"], by="x", base_url="https://e.test")


def test_send_mass_remind_not_found(db):
    with pytest.raises(ValueError, match="no encontrado"):
        wr.send_mass_remind(db, "000000000000000000000000", by="x", base_url="https://e.test")


def test_send_mass_remind_rejects_drawn_raffle(db, raffle, monkeypatch):
    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    db.raffles.update_one({"_id": oid(raffle["id"])}, {"$set": {"status": "drawn"}})
    with pytest.raises(ValueError, match="no admite"):
        wr.send_mass_remind(db, raffle["id"], by="x", base_url="https://e.test")


def test_send_mass_remind_no_phones(db, raffle, monkeypatch):
    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    with pytest.raises(ValueError, match="teléfono"):
        wr.send_mass_remind(db, raffle["id"], by="x", base_url="https://e.test")


def test_mass_remind_blocked_within_24h(db, raffle, monkeypatch):
    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    rid = raffle["id"]
    db.audit_log.insert_one({
        "at": datetime.now(timezone.utc),
        "action": wr.MASS_REMIND_ACTION,
        "by": "admin",
        "raffle_id": str(rid),
        "folio": None,
        "sent": 1,
        "failed": 0,
        "recipients": 1,
    })
    with pytest.raises(ValueError, match="24 horas"):
        wr.send_mass_remind(db, rid, by="admin", base_url="https://e.test")


def test_mass_remind_allows_after_cooldown_window(db, raffle, monkeypatch):
    from app.services import raffle_service as rs

    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: None)
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)
    rid = raffle["id"]
    rs.entregar_boleto(db, rid, 1, name="Ana", phone="5511223344")
    db.audit_log.insert_one({
        "at": datetime.now(timezone.utc) - timedelta(hours=25),
        "action": wr.MASS_REMIND_ACTION,
        "by": "admin",
        "raffle_id": str(rid),
        "folio": None,
        "sent": 1,
        "failed": 0,
        "recipients": 1,
    })
    out = wr.send_mass_remind(db, rid, by="admin", base_url="https://e.test")
    assert out["sent"] == 1


def test_mass_remind_sends_unique_with_delay_and_audit(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin,
                json={"name": "Ana", "phone": "5511223344"})
    calls = []
    sleeps = []
    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: calls.append(kw))
    monkeypatch.setattr(wr.time, "sleep", lambda s: sleeps.append(s))
    out = wr.send_mass_remind(db, rid, by="admin", base_url="https://e.test")
    assert out["sent"] == 1 and out["recipients"] == 1
    assert calls[0]["jid"] == "525511223344"
    assert any(a["action"] == "mass_remind" for a in db.audit_log.find())


def test_mass_remind_counts_failed_without_aborting(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin,
                json={"name": "Ana", "phone": "5511223344"})
    client.post(f"/api/v1/raffles/{rid}/tickets/2/assign", headers=admin,
                json={"name": "Beto", "phone": "5599887766"})

    def fake_send(**kw):
        if kw["jid"] == "525599887766":
            raise RuntimeError("boom")

    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    monkeypatch.setattr(wr, "send_text_evolution", fake_send)
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)
    out = wr.send_mass_remind(db, rid, by="admin", base_url="https://e.test")
    assert out["sent"] == 1
    assert out["failed"] == 1
    assert out["recipients"] == 2
    assert out["skipped_no_phone"] == 0


def test_mass_remind_counts_skipped_no_phone(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    # folio 1 assigned with phone; folio 2 has a participant without a usable phone
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin,
                json={"name": "Ana", "phone": "5511223344"})
    db.tickets.update_one(
        {"raffle_id": oid(rid), "folio": 2},
        {"$set": {"status": "delivered", "participant": {"name": "Beto", "phone": ""}}},
    )
    monkeypatch.setattr(wr, "get_settings", lambda: _settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: None)
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)
    out = wr.send_mass_remind(db, rid, by="admin", base_url="https://e.test")
    assert out["recipients"] == 1
    assert out["skipped_no_phone"] == 1


def test_send_text_evolution_raises_runtime_error_on_http_error(monkeypatch):
    def fake_post(*args, **kwargs):
        req = httpx.Request("POST", "http://evo/message/sendText/i")
        return httpx.Response(500, request=req)

    monkeypatch.setattr(wr.httpx, "post", fake_post)
    with pytest.raises(RuntimeError):
        wr.send_text_evolution(
            url="http://evo", api_key="k", instance="i",
            jid="525511223344", text="hola",
        )


def test_send_text_evolution_ok(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req)

    monkeypatch.setattr(wr.httpx, "post", fake_post)
    wr.send_text_evolution(
        url="http://evo/", api_key="k", instance="sorteo",
        jid="525511223344", text="hola",
    )
    assert captured["url"] == "http://evo/message/sendText/sorteo"
    assert captured["headers"] == {"apikey": "k"}
    assert captured["json"] == {"number": "525511223344", "text": "hola"}


def test_constants():
    assert wr.MASS_REMIND_ACTION == "mass_remind"
    assert wr.COOLDOWN_HOURS == 24
    assert wr.DELAY_SECONDS == 1.5


def test_mass_remind_endpoint_admin_ok(client, admin, db, raffle, monkeypatch):
    import app.services.whatsapp_reminder as wr
    rid = raffle["id"]
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin,
                json={"name": "Ana", "phone": "5511223344"})
    monkeypatch.setattr(wr, "get_settings", lambda: type("S", (), {
        "evolution_url": "http://evo:8080", "evolution_api_key": "k",
        "evolution_instance": "i"})())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: None)
    monkeypatch.setattr("app.services.whatsapp_reminder.time.sleep", lambda s: None)
    r = client.post(f"/api/v1/raffles/{rid}/remind", headers=admin,
                    json={"base_url": "https://e.test"})
    assert r.status_code == 200
    assert r.json()["sent"] == 1


def test_mass_remind_forbidden_without_admin(client, raffle):
    # sin token: 401; con require_admin cualquier no-admin da 403
    r = client.post(f"/api/v1/raffles/{raffle['id']}/remind", json={"base_url": "https://e.test"})
    assert r.status_code in (401, 403)
