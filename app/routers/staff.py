"""Rutas del dashboard (staff): sorteos, boletos, sorteo, exportación."""
from __future__ import annotations

import io
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.db import get_db
from app.schemas import (
    ChangePasswordIn,
    RaffleCreate,
    RaffleOut,
    RaffleStats,
    TicketAssign,
    TicketOut,
    TicketPaidIn,
)
from app.security import CurrentUser, require_admin, require_staff
from app.services import raffle_service as rs

router = APIRouter(prefix="/api/v1", tags=["staff"])


@router.get("/me")
def me(user: Annotated[CurrentUser, Depends(require_staff)]) -> dict:
    return {"sub": user["sub"], "role": user["role"], "name": user["name"]}


@router.post("/change-password")
def change_password(
    body: ChangePasswordIn,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> dict:
    """Cambia la contraseña del usuario autenticado."""
    from bson import ObjectId

    from app.security import hash_password, verify_password

    db = get_db()
    try:
        doc = db.users.find_one({"_id": ObjectId(user["sub"])})
    except Exception:
        doc = None
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario no encontrado")

    if not verify_password(body.current_password, doc.get("password_hash", "")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La contraseña actual es incorrecta")

    if verify_password(body.new_password, doc.get("password_hash", "")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "La nueva contraseña debe ser diferente a la actual",
        )

    db.users.update_one(
        {"_id": doc["_id"]},
        {"$set": {"password_hash": hash_password(body.new_password)}},
    )
    return {"ok": True, "message": "Contraseña actualizada correctamente"}


# ---------- Sorteos ----------
@router.get("/raffles", response_model=list[RaffleOut])
def list_raffles(user: Annotated[CurrentUser, Depends(require_staff)]) -> list[dict]:
    return rs.list_raffles(get_db())


@router.post("/raffles", response_model=RaffleOut, status_code=status.HTTP_201_CREATED)
def create_raffle(
    body: RaffleCreate,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    try:
        return rs.crear_sorteo(
            get_db(),
            title=body.title,
            prize=body.prize,
            prize_value=body.prize_value,
            price_min=body.price_min,
            price_max=body.price_max,
            draw_date=body.draw_date,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/raffles/{raffle_id}", response_model=RaffleOut)
def get_raffle(
    raffle_id: str,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> dict:
    doc = rs.get_raffle(get_db(), raffle_id)
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sorteo no encontrado")
    return doc


@router.get("/raffles/{raffle_id}/stats", response_model=RaffleStats)
def raffle_stats(
    raffle_id: str,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> dict:
    if not rs.get_raffle(get_db(), raffle_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sorteo no encontrado")
    return rs.stats_sorteo(get_db(), raffle_id)


@router.get("/raffles/{raffle_id}/tickets", response_model=list[TicketOut])
def list_tickets(
    raffle_id: str,
    user: Annotated[CurrentUser, Depends(require_staff)],
    status_filter: Annotated[Optional[str], Query(alias="status")] = None,
) -> list[dict]:
    if not rs.get_raffle(get_db(), raffle_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sorteo no encontrado")
    return rs.list_tickets(get_db(), raffle_id, status_filter)


@router.post("/raffles/{raffle_id}/tickets/{folio}/assign", response_model=TicketOut)
def assign_ticket(
    raffle_id: str,
    folio: int,
    body: TicketAssign,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> dict:
    try:
        doc = rs.entregar_boleto(get_db(), raffle_id, folio, body.name, body.phone)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Boleto no encontrado")
    return doc


@router.post("/raffles/{raffle_id}/tickets/{folio}/pay", response_model=TicketOut)
def mark_paid(
    raffle_id: str,
    folio: int,
    body: TicketPaidIn,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> dict:
    try:
        doc = rs.marcar_pagado(get_db(), raffle_id, folio, body.note)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Boleto no encontrado")
    return doc


@router.post("/raffles/{raffle_id}/tickets/{folio}/release", response_model=TicketOut)
def release_ticket(
    raffle_id: str,
    folio: int,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> dict:
    try:
        doc = rs.liberar_boleto(get_db(), raffle_id, folio)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Boleto no encontrado")
    return doc


@router.post("/raffles/{raffle_id}/close", response_model=RaffleOut)
def close_raffle(
    raffle_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    try:
        return rs.close_raffle(get_db(), raffle_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/raffles/{raffle_id}/draw")
def draw(
    raffle_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    try:
        return rs.run_draw(get_db(), raffle_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ---------- Exportación Excel ----------
@router.get("/raffles/{raffle_id}/export/participants")
def export_participants(
    raffle_id: str,
    user: Annotated[CurrentUser, Depends(require_staff)],
) -> StreamingResponse:
    """Exporta la lista de participantes y boletos a un archivo .xlsx."""
    from datetime import datetime, timezone

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    db = get_db()
    raffle = rs.get_raffle(db, raffle_id)
    if not raffle:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sorteo no encontrado")

    tickets = rs.list_tickets(db, raffle_id)
    stats = rs.stats_sorteo(db, raffle_id)

    wb = Workbook()

    # ---- Hoja 1: Participantes ----
    ws = wb.active
    ws.title = "Participantes"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1B5E20")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin", color="D1D5DB"),
        right=Side(style="thin", color="D1D5DB"),
        top=Side(style="thin", color="D1D5DB"),
        bottom=Side(style="thin", color="D1D5DB"),
    )
    money_fmt = '"$"#,##0'

    headers = [
        "Folio",
        "Nombre",
        "Teléfono",
        "Monto",
        "Estado",
        "Código",
        "Registrado",
        "Raspado",
        "Pagado",
        "Ganador",
    ]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    status_labels = {
        "free": "Libre",
        "delivered": "Entregado",
        "registered": "Registrado",
        "scratched": "Raspado",
        "paid": "Pagado",
        "released": "Liberado",
    }
    winner_folio = (raffle.get("winner") or {}).get("folio")

    def _fmt_date(val):
        if not val:
            return ""
        if isinstance(val, str):
            return val[:16].replace("T", " ")
        return val.strftime("%d/%m/%Y %H:%M")

    row = 2
    for t in tickets:
        p = t.get("participant") or {}
        is_winner = t.get("folio") == winner_folio and raffle.get("status") == "drawn"
        values = [
            t.get("folio"),
            p.get("name") or "",
            p.get("phone") or "",
            t.get("amount"),
            status_labels.get(t.get("status"), t.get("status")),
            t.get("access_code"),
            _fmt_date(t.get("registered_at")),
            _fmt_date(t.get("scratched_at")),
            _fmt_date(t.get("paid_at")),
            "🏆 SÍ" if is_winner else "",
        ]
        for col, v in enumerate(values, 1):
            cell = ws.cell(row=row, column=col, value=v)
            cell.border = thin_border
            if col == 4 and isinstance(v, (int, float)):
                cell.number_format = money_fmt
            if is_winner:
                cell.fill = PatternFill("solid", fgColor="FFF8E1")
            if t.get("status") == "paid" and not is_winner:
                cell.fill = PatternFill("solid", fgColor="F1F8F2")
        row += 1

    # Ajuste de anchos
    widths = [8, 28, 16, 10, 14, 10, 18, 18, 18, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:J{row - 1}"

    # ---- Hoja 2: Resumen ----
    ws2 = wb.create_sheet("Resumen")
    ws2.column_dimensions["A"].width = 32
    ws2.column_dimensions["B"].width = 22

    title_cell = ws2.cell(row=1, column=1, value=f"Sorteo: {raffle['title']}")
    title_cell.font = Font(bold=True, size=14, color="1B5E20")

    ws2.cell(row=2, column=1, value=f"Premio: {raffle['prize']}").font = Font(size=11)
    ws2.cell(row=3, column=1, value=f"Valor del premio: ${raffle['prize_value']} MXN").font = Font(size=11)
    ws2.cell(row=4, column=1, value=f"Rango de precios: ${raffle['price_min']} – ${raffle['price_max']}").font = Font(size=11)
    ws2.cell(row=5, column=1, value=f"Estado: {status_labels.get(raffle['status'], raffle['status'])}").font = Font(size=11)
    ws2.cell(row=6, column=1, value=f"Generado: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC").font = Font(size=9, italic=True)

    summary = [
        ("Total de boletos", stats["total_tickets"]),
        ("Libres", stats["free"]),
        ("Entregados", stats["delivered"]),
        ("Registrados", stats["registered"]),
        ("Raspados", stats["scratched"]),
        ("Pagados", stats["paid"]),
        ("Participantes únicos", stats["participants"]),
        ("Recaudación esperada", stats["revenue_expected"]),
        ("Recaudación confirmada", stats["revenue_confirmed"]),
    ]
    r = 8
    for label, val in summary:
        c1 = ws2.cell(row=r, column=1, value=label)
        c1.font = Font(bold=True, size=11)
        c2 = ws2.cell(row=r, column=2, value=val)
        c2.font = Font(size=11)
        if "Recaudación" in label:
            c2.number_format = money_fmt
        r += 1

    if raffle.get("status") == "drawn" and raffle.get("winner"):
        w = raffle["winner"]
        ws2.cell(row=r + 1, column=1, value="🏆 GANADOR").font = Font(bold=True, size=12, color="8D6E00")
        ws2.cell(row=r + 2, column=1, value=f"Folio {w.get('folio')}").font = Font(bold=True, size=11)
        pname = (w.get("participant") or {}).get("name") or "—"
        ws2.cell(row=r + 3, column=1, value=f"Nombre: {pname}").font = Font(size=11)
        ws2.cell(row=r + 4, column=1, value=f"Monto del boleto: ${w.get('amount')}").font = Font(size=11)

    # Generar bytes
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    slug = raffle.get("slug", "sorteo")
    filename = f"participantes-{slug}.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
