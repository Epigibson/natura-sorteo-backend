"""Publicación en TikTok vía Content Posting API v2 (preparado para OAuth/Token oficial)."""
from __future__ import annotations

from typing import Literal

import httpx
from pymongo.database import Database

from app.services import app_settings as aps
from app.services import instagram_publisher as igp
from app.services.raffle_service import audit, get_raffle, oid

TIKTOK_PUBLISH_ACTION = "tiktok_publish"
TIKTOK_API_URL = "https://open.tiktokapis.com/v2/post/publish/content/init/"

TikTokPostKind = Literal["launch", "scarcity", "winner"]
TikTokPrivacy = Literal["SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"]


def build_tiktok_copy(raffle: dict, kind: TikTokPostKind, *, paid_count: int = 0) -> tuple[str, str]:
    """Devuelve (title_corto, description) optimizados para TikTok Photo Post."""
    title = raffle.get("title") or "Sorteo Natura"
    prize = raffle.get("prize") or "Premio Natura"
    prize_value = raffle.get("prize_value") or 0
    price_min = raffle.get("price_min") or 30
    price_max = raffle.get("price_max") or 50
    ticket_count = int(raffle.get("ticket_count") or 21)
    free_count = max(0, ticket_count - paid_count)

    tags = "#RifasNatura #NaturaMexico #RaspaditoDigital #Queretaro #Sorteo"
    if kind == "launch":
        short_title = f"🎁 {prize} desde ${price_min} MXN ✨"[:90]
        desc = (
            f"Raspadito digital ({title}) · Valor en catálogo ${prize_value} MXN 🔥 "
            f"Solo {ticket_count} boletos de ${price_min} a ${price_max} MXN. "
            f"Link en la bio para escoger tu folio 🍀 {tags}"
        )
    elif kind == "scarcity":
        short_title = f"🚨 Últimos {free_count} boletos · {prize}"[:90]
        desc = (
            f"Ya se fueron {paid_count} de {ticket_count} boletos en {title}. "
            f"Entra al link de la bio y raspa el tuyo antes de cerrar ⏳ {tags}"
        )
    else:
        winner = raffle.get("winner") or {}
        w_folio = winner.get("folio") or "—"
        short_title = f"🏆 Ganador folio #{w_folio} · {title}"[:90]
        desc = f"Gracias a todos por participar en {title}. ¡Pronto nuevo raspadito Natura! 💚 {tags}"
    return short_title, desc


def post_photo_to_tiktok_api(
    *,
    access_token: str,
    image_url: str,
    title: str,
    description: str,
    privacy_level: TikTokPrivacy = "SELF_ONLY",
) -> str:
    """Inicia una publicación de foto en TikTok Content Posting API v2. Devuelve publish_id."""
    payload = {
        "media_type": "PHOTO",
        "post_mode": "DIRECT_POST",
        "post_info": {
            "title": title,
            "description": description,
            "privacy_level": privacy_level,
            "disable_comment": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": [image_url],
        },
    }
    try:
        r = httpx.post(
            TIKTOK_API_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        err = body.get("error") or {}
        if err.get("code") and err.get("code") != "ok":
            raise RuntimeError(f"TikTok API {err.get('code')}: {err.get('message', '')}")
        return str((body.get("data") or {}).get("publish_id") or "")
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"TikTok HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"TikTok error: {exc}") from exc


def publish_raffle_tiktok(
    db: Database,
    raffle_id: str,
    *,
    kind: TikTokPostKind = "launch",
    by: str = "system",
    image_base64: str | None = None,
    privacy_level: TikTokPrivacy = "SELF_ONLY",
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")

    token = aps.get_tiktok_token(db)
    if not token:
        raise ValueError("TikTok no está configurado (pega tu TikTok Access Token en Configuración)")

    rid = oid(raffle_id)
    paid_count = db.tickets.count_documents({"raffle_id": rid, "status": "paid"})
    short_title, description = build_tiktok_copy(raffle, kind, paid_count=paid_count)

    if image_base64 and image_base64.strip():
        image_url = igp.upload_promo_to_cloudinary(image_base64, public_id_prefix=f"tt-{raffle_id}-{kind}")
    else:
        image_url = str(raffle.get("image_url") or "").strip()

    if not image_url or not image_url.startswith("https://"):
        raise ValueError("TikTok requiere una imagen pública HTTPS")

    publish_id = post_photo_to_tiktok_api(
        access_token=token,
        image_url=image_url,
        title=short_title,
        description=description,
        privacy_level=privacy_level,
    )
    audit(
        db,
        TIKTOK_PUBLISH_ACTION,
        by=by,
        raffle_id=str(raffle_id),
        kind=kind,
        publish_id=publish_id,
        privacy_level=privacy_level,
        status="ok",
    )
    return {
        "ok": True,
        "kind": kind,
        "publish_id": publish_id,
        "privacy_level": privacy_level,
        "description": description,
    }
