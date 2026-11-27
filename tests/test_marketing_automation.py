from app.services import app_settings as aps
from app.services import facebook_publisher as fbp
from app.services import marketing_service as mks
from app.services import raffle_service as rs
from app.services import whatsapp_reminder as wr


def _evo_settings(url="http://evo:8080", key="k", instance="sorteo"):
    return type("S", (), {
        "evolution_url": url,
        "evolution_api_key": key,
        "evolution_instance": instance,
        "auto_release_hours": 48,
        "fb_page_id": "",
        "fb_page_access_token": "",
        "public_base_url": "",
    })()


def test_payment_confirmed_sends_referral_1plus1(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    sent_calls = []
    monkeypatch.setattr(mks, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: sent_calls.append(kw))

    client.post(
        f"/api/v1/raffles/{rid}/tickets/1/assign",
        headers=admin,
        json={"name": "Ana Lopez", "phone": "5511223344"},
    )
    res = client.post(
        f"/api/v1/raffles/{rid}/tickets/1/pay",
        headers={**admin, "Origin": "https://sorteo.test"},
        json={},
    )
    assert res.status_code == 200
    assert len(sent_calls) == 1
    assert sent_calls[0]["jid"] == "525511223344"
    assert "1+1" in sent_calls[0]["text"]
    assert f"https://sorteo.test/sorteo/{raffle['slug']}" in sent_calls[0]["text"]


def test_unpaid_reminders_groups_folios_and_enforces_cooldown(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    sent_calls = []
    monkeypatch.setattr(wr, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: sent_calls.append(kw))
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)

    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin, json={"name": "Ana", "phone": "5511223344"})
    client.post(f"/api/v1/raffles/{rid}/tickets/2/assign", headers=admin, json={"name": "Ana", "phone": "5511223344"})
    client.post(f"/api/v1/raffles/{rid}/tickets/3/assign", headers=admin, json={"name": "Beto", "phone": "5599887766"})
    client.post(f"/api/v1/raffles/{rid}/tickets/3/pay", headers=admin, json={})
    sent_calls.clear()

    r = client.post(
        f"/api/v1/raffles/{rid}/marketing/remind-unpaid",
        headers=admin,
        json={"base_url": "https://sorteo.test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == 1
    assert body["recipients"] == 1
    assert "#01" in sent_calls[0]["text"] and "#02" in sent_calls[0]["text"]

    # Segunda llamada inmediata bloqueada por cooldown de 12h
    r2 = client.post(
        f"/api/v1/raffles/{rid}/marketing/remind-unpaid",
        headers=admin,
        json={"base_url": "https://sorteo.test"},
    )
    assert r2.status_code == 400
    assert "12 horas" in r2.json()["detail"]


def test_post_draw_campaign_notifies_winner_and_non_winners(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    sent_calls = []
    monkeypatch.setattr(wr, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: sent_calls.append(kw))
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)
    # Desactivar auto-disparo en draw para probar el endpoint manual limpiamente
    aps.save_marketing_settings(db, {"auto_wa_on_draw": False, "auto_wa_on_pay": False})

    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin, json={"name": "Ana", "phone": "5511223344"})
    client.post(f"/api/v1/raffles/{rid}/tickets/2/assign", headers=admin, json={"name": "Beto", "phone": "5599887766"})
    client.post(f"/api/v1/raffles/{rid}/tickets/1/pay", headers=admin, json={})
    client.post(f"/api/v1/raffles/{rid}/tickets/2/pay", headers=admin, json={})

    d = client.post(f"/api/v1/raffles/{rid}/draw", headers=admin)
    assert d.status_code == 200

    res = client.post(
        f"/api/v1/raffles/{rid}/marketing/post-draw",
        headers=admin,
        json={"base_url": "https://sorteo.test", "bonus_amount": 40, "min_order": 300},
    )
    assert res.status_code == 200
    assert res.json()["sent"] == 2
    texts = {c["jid"]: c["text"] for c in sent_calls}
    assert any("FELICIDADES" in t for t in texts.values())
    assert any("bono de $40 MXN" in t for t in texts.values())

    # No permite enviar dos veces la campaña post-sorteo
    res2 = client.post(
        f"/api/v1/raffles/{rid}/marketing/post-draw",
        headers=admin,
        json={"base_url": "https://sorteo.test"},
    )
    assert res2.status_code == 400


def test_invite_past_participants_excludes_current_raffle(client, admin, db, raffle, monkeypatch):
    rid1 = raffle["id"]
    client.post(f"/api/v1/raffles/{rid1}/tickets/1/assign", headers=admin, json={"name": "Ana", "phone": "5511223344"})
    client.post(f"/api/v1/raffles/{rid1}/tickets/2/assign", headers=admin, json={"name": "Caro", "phone": "5544332211"})

    r2 = rs.crear_sorteo(db, title="Rifa Dos", prize="Crema", prize_value=500, price_min=30, price_max=35)
    rid2 = r2["id"]
    # Ana ya está en Rifa Dos, Caro no
    client.post(f"/api/v1/raffles/{rid2}/tickets/1/assign", headers=admin, json={"name": "Ana", "phone": "5511223344"})

    st = client.get(f"/api/v1/raffles/{rid2}/marketing/status", headers=admin)
    assert st.status_code == 200
    assert st.json()["past_participants_count"] == 1

    sent_calls = []
    monkeypatch.setattr(wr, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: sent_calls.append(kw))
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)

    inv = client.post(
        f"/api/v1/raffles/{rid2}/marketing/invite-past",
        headers=admin,
        json={"base_url": "https://sorteo.test"},
    )
    assert inv.status_code == 200
    assert inv.json()["recipients"] == 1
    assert sent_calls[0]["jid"] == "525544332211"
    assert "¡Hola Caro!" in sent_calls[0]["text"]
    st2 = client.get(f"/api/v1/raffles/{rid2}/marketing/status", headers=admin)
    assert st2.json()["past_invite_job"]["status"] == "completed"
    assert st2.json()["past_invite_job"]["sent"] == 1
    assert st2.json()["past_participants_count"] == 0


def test_send_batch_stops_early_on_428_disconnect(monkeypatch):
    calls = []

    def fake_send(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise RuntimeError("Evolution HTTP 428 Connection Closed")

    monkeypatch.setattr(wr, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_text_evolution", fake_send)
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)

    sent, failed = mks._send_batch(
        ["5511111111", "5522222222", "5533333333", "5544444444"],
        "Hola",
        delay_seconds=0,
        stop_on_disconnect=True,
    )
    assert sent == 1
    assert failed == 3
    assert len(calls) == 2


def test_send_batch_does_not_abort_on_http_400_bad_numbers(monkeypatch):
    calls = []

    def fake_send(**kw):
        calls.append(kw["jid"])
        if kw["jid"] in ("521111111111", "521222222222", "521333333333"):
            raise RuntimeError("Evolution HTTP 400")

    monkeypatch.setattr(wr, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_text_evolution", fake_send)
    monkeypatch.setattr(wr.time, "sleep", lambda s: None)

    sent, failed = mks._send_batch(
        ["1111111111", "1222222222", "1333333333", "5544444444"],
        "Hola",
        delay_seconds=0,
        stop_on_disconnect=True,
    )
    assert sent == 1
    assert failed == 3
    assert len(calls) == 4


def test_facebook_publish_and_milestone_auto_trigger(client, admin, db, monkeypatch):
    # Configurar FB desde settings API y verificar que token no se filtra en GET
    put = client.put(
        "/api/v1/marketing/settings",
        headers=admin,
        json={
            "auto_wa_on_pay": False,
            "auto_wa_on_draw": False,
            "auto_fb_on_create": False,
            "auto_fb_milestones": True,
            "public_base_url": "https://sorteo.test",
            "fb_page_id": "123456789",
            "fb_page_access_token": "secret-fb-token",
        },
    )
    assert put.status_code == 200
    assert put.json()["fb_token_configured"] is True
    assert "fb_page_access_token" not in put.json()

    # Crear sorteo de 4 boletos ($30..$33) para probar hito 50% (2 boletos) y 80% (4 boletos)
    r = rs.crear_sorteo(db, title="Rifa FB", prize="Set Ekos", prize_value=600, price_min=30, price_max=33)
    rid = r["id"]

    fb_calls = []
    comment_calls = []
    monkeypatch.setattr(fbp, "post_to_facebook_graph", lambda **kw: fb_calls.append(kw) or "post_999")
    monkeypatch.setattr(fbp, "post_comment_to_facebook_graph", lambda **kw: comment_calls.append(kw) or "comment_111")

    # Publicación manual de lanzamiento con imagen promo en base64
    sample_png_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    pub = client.post(
        f"/api/v1/raffles/{rid}/marketing/facebook",
        headers=admin,
        json={"kind": "launch", "base_url": "https://sorteo.test", "image_base64": sample_png_b64},
    )
    assert pub.status_code == 200
    assert pub.json()["post_id"] == "post_999"
    assert pub.json()["comment_id"] == "comment_111"
    assert len(fb_calls) == 1
    assert "Set Ekos" in fb_calls[0]["message"]
    assert "primer comentario" in fb_calls[0]["message"]
    assert "https://sorteo.test" not in fb_calls[0]["message"]
    assert fb_calls[0]["image_bytes"] is not None and fb_calls[0]["image_bytes"].startswith(b"\x89PNG")
    assert len(comment_calls) == 1
    assert comment_calls[0]["post_id"] == "post_999"
    assert f"https://sorteo.test/sorteo/{r['slug']}" in comment_calls[0]["message"]

    # Cobrar 2 de 4 boletos (50%) -> dispara milestone_50 automático + primer comentario
    for folio in (1, 2):
        client.post(f"/api/v1/raffles/{rid}/tickets/{folio}/assign", headers=admin, json={"name": f"User {folio}", "phone": f"551122330{folio}"})
        client.post(f"/api/v1/raffles/{rid}/tickets/{folio}/pay", headers=admin, json={})

    assert any("mitad" in c["message"] for c in fb_calls)
    assert len(comment_calls) == 2

    # Cobrar 4 de 4 boletos (100% >= 80%) -> dispara milestone_80 automático + primer comentario
    for folio in (3, 4):
        client.post(f"/api/v1/raffles/{rid}/tickets/{folio}/assign", headers=admin, json={"name": f"User {folio}", "phone": f"551122330{folio}"})
        client.post(f"/api/v1/raffles/{rid}/tickets/{folio}/pay", headers=admin, json={})

    assert any("ÚLTIMOS" in c["message"] for c in fb_calls)
    assert len(comment_calls) == 3


def test_whatsapp_vip_group_announcement_and_invite_url(client, admin, db, raffle, monkeypatch):
    rid = raffle["id"]
    client.put(
        "/api/v1/marketing/settings",
        headers=admin,
        json={
            "auto_wa_on_pay": True,
            "auto_wa_on_draw": False,
            "auto_fb_on_create": False,
            "auto_fb_milestones": False,
            "public_base_url": "https://sorteo.test",
            "fb_page_id": "",
            "wa_vip_group_jid": "120363012345678901@g.us",
            "wa_vip_group_invite_url": "https://chat.whatsapp.com/ABC123VIP",
        },
    )

    media_calls = []
    text_calls = []
    monkeypatch.setattr(wr, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(mks, "get_settings", lambda: _evo_settings())
    monkeypatch.setattr(wr, "send_media_evolution", lambda **kw: media_calls.append(kw))
    monkeypatch.setattr(wr, "send_text_evolution", lambda **kw: text_calls.append(kw))

    # 1) Anuncio al Grupo VIP con imagen promo
    sample_png_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    res = client.post(
        f"/api/v1/raffles/{rid}/marketing/whatsapp-group",
        headers=admin,
        json={"kind": "launch", "base_url": "https://sorteo.test", "image_base64": sample_png_b64},
    )
    assert res.status_code == 200
    assert len(media_calls) == 1
    assert media_calls[0]["jid"] == "120363012345678901@g.us"
    assert f"https://sorteo.test/sorteo/{raffle['slug']}" in media_calls[0]["caption"]

    # 2) Confirmación de pago incluye link de invitación voluntaria al Grupo VIP
    client.post(f"/api/v1/raffles/{rid}/tickets/1/assign", headers=admin, json={"name": "Ana Lopez", "phone": "5511223344"})
    pay = client.post(f"/api/v1/raffles/{rid}/tickets/1/pay", headers=admin, json={})
    assert pay.status_code == 200
    assert len(text_calls) == 1
    assert "https://chat.whatsapp.com/ABC123VIP" in text_calls[0]["text"]
