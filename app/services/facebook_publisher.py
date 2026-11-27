"""Publicación automática en Página de Facebook vía Meta Graph API."""
from __future__ import annotations

import base64
from typing import Literal

import httpx
from pymongo.database import Database

from app.services import app_settings as aps
from app.services.raffle_service import audit, get_raffle, oid

FB_PUBLISH_ACTION = "fb_publish"
GRAPH_API_VERSION = "v21.0"

PostKind = Literal["launch", "milestone_50", "milestone_80", "winner"]


def _decode_image_base64(raw: str | None) -> bytes | None:
    if not raw or not raw.strip():
        return None
    data = raw.strip()
    if "," in data and data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        decoded = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError("Imagen promocional inválida (base64 corrupto)") from exc
    if len(decoded) > 4_500_000:
        raise ValueError("Imagen promocional demasiado pesada (máximo 4.5 MB)")
    return decoded


def build_fb_message(
    raffle: dict,
    kind: PostKind,
    base_url: str,
    *,
    paid_count: int = 0,
) -> tuple[str, str]:
    """Construye (mensaje_sin_enlace, link_publico) para la Página de Facebook.

    Regla de alcance orgánico en Facebook: el texto principal NO lleva URL;
    el enlace se publica automáticamente en el primer comentario.
    """
    title = raffle.get("title") or "Sorteo Natura"
    prize = raffle.get("prize") or "Premio Natura"
    prize_value = raffle.get("prize_value") or 0
    price_min = raffle.get("price_min") or 30
    price_max = raffle.get("price_max") or 50
    ticket_count = int(raffle.get("ticket_count") or 21)
    free_count = max(0, ticket_count - paid_count)
    draw_date = raffle.get("draw_date") or "en vivo próximamente"
    slug = raffle.get("slug") or ""
    clean_base = (base_url or "").strip().rstrip("/")
    landing_url = f"{clean_base}/sorteo/{slug}" if clean_base else ""

    if kind == "launch":
        msg = (
            f"🎁 ¡Llévate este {prize} desde ${price_min} pesitos! ✨\n\n"
            f"Valor en catálogo: ${prize_value} MXN 🔥\n\n"
            f"🎰 Dinámica de raspadito digital ({title}):\n"
            f"🔹 Solo {ticket_count} boletos disponibles\n"
            f"🔹 Cada boleto esconde un precio único entre ${price_min} y ${price_max} MXN\n"
            f"🔹 Pagues ${price_min} o ${price_max}, todos tienen la misma probabilidad de ganar 🍀\n"
            f"🔹 Sorteo EN VIVO: {draw_date}\n\n"
            f"👇 Entra al link del primer comentario, escoge tu folio y raspa con tu dedo.\n"
            f'💬 Comenta "YO" si quieres que te mande el enlace por inbox.'
        )
    elif kind == "milestone_50":
        msg = (
            f"🔥 ¡Ya vamos a más de la mitad en {title}! ({paid_count}/{ticket_count} boletos pagados)\n\n"
            f"🎁 Premio: {prize} (valor ${prize_value} MXN)\n"
            f"🎟️ Quedan {free_count} lugares disponibles desde ${price_min} MXN.\n"
            f"Recuerda que no importa si te sale ${price_min} o ${price_max}: todos valen igual 🍀\n\n"
            f"👇 Entra al link del primer comentario y aparta el tuyo antes de que se acaben."
        )
    elif kind == "milestone_80":
        msg = (
            f"⏳ ¡ÚLTIMOS {free_count} BOLETOS de {title}! 🚨\n\n"
            f"🎁 Premio: {prize} (valor ${prize_value} MXN)\n"
            f"Ya hay {paid_count} de {ticket_count} boletos pagados. ¡Casi cerramos venta!\n\n"
            f"👇 Corre al link del primer comentario por tu folio antes de que vuelen."
        )
    else:  # winner
        winner = raffle.get("winner") or {}
        w_folio = winner.get("folio") or "—"
        w_name = (winner.get("participant") or {}).get("name") or "Participante"
        folio_txt = f"#{w_folio:02d}" if isinstance(w_folio, int) else f"#{w_folio}"
        msg = (
            f"🏆 ¡TENEMOS GANADOR en {title}! 🎉\n\n"
            f"El premio ({prize}) se fue con el folio {folio_txt} — {w_name} 🍀✨\n"
            f"Gracias a todos los que participaron. ¡Muy pronto lanzamos el siguiente raspadito Natura! 💚"
        )

    return msg, landing_url


def build_fb_first_comment(kind: PostKind, landing_url: str) -> str:
    """Construye el texto del primer comentario automático con el enlace al tablero."""
    if not landing_url or kind == "winner":
        return ""
    if kind == "milestone_80":
        return f"🚨 ¡Últimos lugares! Escoge tu folio y raspa tu boleto aquí 👉 {landing_url}"
    return f"👉 Participa aquí, escoge tu folio y raspa tu boleto: {landing_url}"


def post_to_facebook_graph(
    *,
    page_id: str,
    access_token: str,
    message: str,
    link: str = "",
    image_url: str = "",
    image_bytes: bytes | None = None,
) -> str:
    """Publica en /{page_id}/photos (bytes o url) o /{page_id}/feed. Devuelve post_id."""
    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}"
    try:
        if image_bytes:
            r = httpx.post(
                f"{base}/photos",
                data={"caption": message, "access_token": access_token},
                files={"source": ("promo.png", image_bytes, "image/png")},
                timeout=45,
            )
        elif image_url:
            r = httpx.post(
                f"{base}/photos",
                data={"url": image_url, "caption": message, "access_token": access_token},
                timeout=30,
            )
        else:
            payload: dict[str, str] = {"message": message, "access_token": access_token}
            if link:
                payload["link"] = link
            r = httpx.post(f"{base}/feed", data=payload, timeout=30)
        r.raise_for_status()
        body = r.json()
        return str(body.get("post_id") or body.get("id") or "")
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = exc.response.text[:200]
        raise RuntimeError(f"Facebook HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Facebook error: {exc}") from exc


def post_comment_to_facebook_graph(
    *,
    post_id: str,
    access_token: str,
    message: str,
) -> str:
    """Publica el primer comentario automático en /{post_id}/comments. Devuelve comment_id."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{post_id}/comments"
    try:
        r = httpx.post(
            url,
            data={"message": message, "access_token": access_token},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        return str(body.get("id") or "")
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = exc.response.text[:200]
        raise RuntimeError(f"Facebook comment HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Facebook comment error: {exc}") from exc


def already_published(db: Database, raffle_id: str, kind: PostKind) -> bool:
    return (
        db.audit_log.find_one(
            {"action": FB_PUBLISH_ACTION, "raffle_id": str(raffle_id), "kind": kind, "status": "ok"}
        )
        is not None
    )


def publish_raffle_post(
    db: Database,
    raffle_id: str,
    *,
    kind: PostKind = "launch",
    base_url: str = "",
    by: str = "system",
    force: bool = False,
    image_base64: str | None = None,
) -> dict:
    raffle = get_raffle(db, raffle_id)
    if not raffle:
        raise ValueError("Sorteo no encontrado")

    page_id, token, cfg_base = aps.get_fb_credentials(db)
    if not page_id or not token:
        raise ValueError("Facebook no está configurado (falta Page ID o Access Token)")

    if not force and already_published(db, raffle_id, kind):
        raise ValueError(f"La publicación '{kind}' ya se realizó para este sorteo")

    rid = oid(raffle_id)
    paid_count = db.tickets.count_documents({"raffle_id": rid, "status": "paid"})
    effective_base = (base_url or "").strip().rstrip("/") or cfg_base
    message, link = build_fb_message(raffle, kind, effective_base, paid_count=paid_count)
    first_comment = build_fb_first_comment(kind, link)
    image_bytes = _decode_image_base64(image_base64)
    image_url = str(raffle.get("image_url") or "").strip()

    post_id = post_to_facebook_graph(
        page_id=page_id,
        access_token=token,
        message=message,
        link="" if first_comment else link,
        image_url=image_url,
        image_bytes=image_bytes,
    )

    comment_id = ""
    comment_error = ""
    if post_id and first_comment:
        try:
            comment_id = post_comment_to_facebook_graph(
                post_id=post_id,
                access_token=token,
                message=first_comment,
            )
        except Exception as exc:
            comment_error = str(exc)[:300]

    audit_extra: dict[str, str] = {
        "raffle_id": str(raffle_id),
        "kind": kind,
        "post_id": post_id,
        "status": "ok",
    }
    if comment_id:
        audit_extra["comment_id"] = comment_id
    if comment_error:
        audit_extra["comment_error"] = comment_error
    audit(db, FB_PUBLISH_ACTION, by=by, **audit_extra)

    return {
        "ok": True,
        "kind": kind,
        "post_id": post_id,
        "comment_id": comment_id,
        "comment_error": comment_error,
        "message": message,
        "first_comment": first_comment,
    }


def safe_auto_publish_event(
    db: Database,
    raffle_id: str,
    *,
    kind: PostKind,
    base_url: str = "",
    by: str = "auto",
) -> None:
    """Wrapper seguro para BackgroundTasks: nunca lanza excepciones hacia el endpoint."""
    try:
        settings = aps.get_marketing_settings(db)
        if kind == "launch" and not settings.get("auto_fb_on_create"):
            return
        if kind in ("milestone_50", "milestone_80") and not settings.get("auto_fb_milestones"):
            return
        page_id, token, _ = aps.get_fb_credentials(db)
        if not page_id or not token:
            return
        if already_published(db, raffle_id, kind):
            return
        publish_raffle_post(db, raffle_id, kind=kind, base_url=base_url, by=by, force=False)
    except Exception as exc:
        audit(
            db,
            FB_PUBLISH_ACTION,
            by=by,
            raffle_id=str(raffle_id),
            kind=kind,
            status="error",
            error=str(exc)[:300],
        )


def check_and_publish_milestone(
    db: Database,
    raffle_id: str,
    *,
    base_url: str = "",
    by: str = "auto",
) -> None:
    """Verifica si el sorteo alcanzó 50% o 80% de boletos pagados y publica en Facebook."""
    try:
        settings = aps.get_marketing_settings(db)
        if not settings.get("auto_fb_milestones"):
            return
        raffle = get_raffle(db, raffle_id)
        if not raffle or raffle.get("status") not in ("open", "closed"):
            return
        total = int(raffle.get("ticket_count") or 0)
        if total <= 0:
            return
        paid = db.tickets.count_documents({"raffle_id": oid(raffle_id), "status": "paid"})
        ratio = paid / total
        if ratio >= 0.8:
            safe_auto_publish_event(db, raffle_id, kind="milestone_80", base_url=base_url, by=by)
        elif ratio >= 0.5:
            safe_auto_publish_event(db, raffle_id, kind="milestone_50", base_url=base_url, by=by)
    except Exception:
        pass
