"""Publicación automática en Instagram Profesional vía Meta Graph API."""
from __future__ import annotations

import base64
from typing import Literal

import httpx
from pymongo.database import Database

from app.config import get_settings
from app.services import app_settings as aps
from app.services.raffle_service import audit, get_raffle, oid

IG_PUBLISH_ACTION = "ig_publish"
GRAPH_API_VERSION = "v21.0"

IgPostKind = Literal["launch", "milestone_50", "milestone_80", "winner"]


def upload_promo_to_cloudinary(image_base64: str, *, public_id_prefix: str = "promo") -> str:
    """Sube una imagen PNG base64 a Cloudinary y devuelve su URL HTTPS pública."""
    import cloudinary
    import cloudinary.uploader

    s = get_settings()
    if not s.cloudinary_cloud_name or not s.cloudinary_api_key or not s.cloudinary_api_secret:
        raise ValueError("Cloudinary no está configurado (necesario para subir la imagen promo a Instagram/TikTok)")

    raw = image_base64.strip()
    if not raw.startswith("data:"):
        # Validar base64 y armar data URI
        try:
            base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise ValueError("Imagen promocional inválida (base64 corrupto)") from exc
        raw = f"data:image/png;base64,{raw}"

    cloudinary.config(
        cloud_name=s.cloudinary_cloud_name,
        api_key=s.cloudinary_api_key,
        api_secret=s.cloudinary_api_secret,
        secure=True,
    )
    res = cloudinary.uploader.upload(
        raw,
        folder="sorteo-natura/social-promos",
        resource_type="image",
        overwrite=True,
    )
    url = str(res.get("secure_url") or "").strip()
    if not url:
        raise RuntimeError("Cloudinary no devolvió secure_url")
    return url


def discover_instagram_account(*, page_id: str, access_token: str) -> dict:
    """Consulta la cuenta de Instagram Profesional vinculada a la Página de Facebook."""
    if not page_id or not access_token:
        raise ValueError("Primero configura Facebook Page ID y Access Token")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}"
    try:
        r = httpx.get(
            url,
            params={
                "fields": "id,name,instagram_business_account{id,username,name}",
                "access_token": access_token,
            },
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = exc.response.text[:200]
        raise RuntimeError(f"Meta HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Error consultando Meta: {exc}") from exc

    ig = body.get("instagram_business_account")
    if not ig or not ig.get("id"):
        raise ValueError(
            "Tu Página de Facebook no tiene una cuenta de Instagram Profesional vinculada "
            "(o falta el permiso instagram_basic en el token)."
        )
    return {
        "ig_user_id": str(ig["id"]),
        "username": str(ig.get("username") or ""),
        "name": str(ig.get("name") or ""),
    }


def build_ig_caption(raffle: dict, kind: IgPostKind, *, paid_count: int = 0) -> str:
    """Construye el caption optimizado para Instagram (llamado al Link en Bio + hashtags)."""
    title = raffle.get("title") or "Sorteo Natura"
    prize = raffle.get("prize") or "Premio Natura"
    prize_value = raffle.get("prize_value") or 0
    price_min = raffle.get("price_min") or 30
    price_max = raffle.get("price_max") or 50
    ticket_count = int(raffle.get("ticket_count") or 21)
    free_count = max(0, ticket_count - paid_count)
    draw_date = raffle.get("draw_date") or "en vivo próximamente"

    hashtags = "\n\n#RifasNatura #NaturaMexico #RaspaditoDigital #SorteoNatura #Queretaro #BellezaNatura"

    if kind == "launch":
        return (
            f"🎁 ¡Llévate este {prize} desde ${price_min} pesitos! ✨\n\n"
            f"Valor en catálogo: ${prize_value} MXN 🔥\n\n"
            f"🎰 Dinámica de raspadito digital ({title}):\n"
            f"🔹 Solo {ticket_count} boletos disponibles\n"
            f"🔹 Cada boleto esconde un precio entre ${price_min} y ${price_max} MXN\n"
            f"🔹 Pagues ${price_min} o ${price_max}, todos tienen la misma probabilidad 🍀\n"
            f"🔹 Sorteo EN VIVO: {draw_date}\n\n"
            f"👇 Entra al LINK EN NUESTRA BIO para escoger tu folio y raspar.\n"
            f'💬 Comenta "YO" y te mando el tablero directo por DM.'
            + hashtags
        )
    if kind == "milestone_50":
        return (
            f"🔥 ¡Ya volaron más de la mitad en {title}! ({paid_count}/{ticket_count} pagados)\n\n"
            f"🎁 Premio: {prize} (valor ${prize_value} MXN)\n"
            f"🎟️ Quedan solo {free_count} lugares desde ${price_min} MXN 🍀\n\n"
            f"👇 Entra al LINK EN NUESTRA BIO antes de que se acaben."
            + hashtags
        )
    if kind == "milestone_80":
        return (
            f"🚨 ¡ÚLTIMOS {free_count} BOLETOS de {title}! ⏳\n\n"
            f"🎁 Premio: {prize} (valor ${prize_value} MXN)\n"
            f"Ya hay {paid_count} de {ticket_count} boletos pagados. ¡Casi cerramos!\n\n"
            f"👇 Corre al LINK EN NUESTRA BIO o comenta YO para mandártelo por DM."
            + hashtags
        )
    winner = raffle.get("winner") or {}
    w_folio = winner.get("folio") or "—"
    w_name = (winner.get("participant") or {}).get("name") or "Participante"
    folio_txt = f"#{w_folio:02d}" if isinstance(w_folio, int) else f"#{w_folio}"
    return (
        f"🏆 ¡TENEMOS GANADOR en {title}! 🎉\n\n"
        f"El premio ({prize}) se fue con el folio {folio_txt} — {w_name} 🍀✨\n"
        f"Gracias a todas por participar. ¡Muy pronto abrimos el siguiente raspadito Natura! 💚"
        + hashtags
    )


def publish_to_instagram_graph(
    *,
    ig_user_id: str,
    access_token: str,
    image_url: str,
    caption: str,
) -> str:
    """Flujo oficial de 2 pasos: crear contenedor media y publicar en Instagram."""
    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}"
    try:
        r1 = httpx.post(
            f"{base}/media",
            data={"image_url": image_url, "caption": caption, "access_token": access_token},
            timeout=30,
        )
        r1.raise_for_status()
        creation_id = str(r1.json().get("id") or "")
        if not creation_id:
            raise RuntimeError("Instagram no devolvió creation_id")

        r2 = httpx.post(
            f"{base}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=30,
        )
        r2.raise_for_status()
        return str(r2.json().get("id") or "")
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = exc.response.text[:200]
        raise RuntimeError(f"Instagram HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Instagram error: {exc}") from exc


def publish_raffle_instagram(
    db: Database,
    raffle_id: str,
    *,
    kind: IgPostKind = "launch",
    by: str = "system",
    image_base64: str | None = None,
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")

    ig_user_id, token = aps.get_ig_credentials(db)
    if not ig_user_id or not token:
        raise ValueError("Instagram no está configurado (falta IG User ID o Access Token en Configuración)")

    rid = oid(raffle_id)
    paid_count = db.tickets.count_documents({"raffle_id": rid, "status": "paid"})
    caption = build_ig_caption(raffle, kind, paid_count=paid_count)

    if image_base64 and image_base64.strip():
        image_url = upload_promo_to_cloudinary(image_base64, public_id_prefix=f"ig-{raffle_id}-{kind}")
    else:
        image_url = str(raffle.get("image_url") or "").strip()

    if not image_url or not image_url.startswith("https://"):
        raise ValueError("Instagram requiere una imagen pública HTTPS (sube foto del premio o genera la promo)")

    media_id = publish_to_instagram_graph(
        ig_user_id=ig_user_id,
        access_token=token,
        image_url=image_url,
        caption=caption,
    )
    audit(
        db,
        IG_PUBLISH_ACTION,
        by=by,
        raffle_id=str(raffle_id),
        kind=kind,
        media_id=media_id,
        status="ok",
    )
    return {"ok": True, "kind": kind, "media_id": media_id, "caption": caption}
