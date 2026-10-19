"""Checklist templates per task kind. The model fills status per item; code never invents facts."""
from __future__ import annotations

CHECKLISTS: dict[str, list[str]] = {
    "trip": [
        "outbound transport booked",
        "lodging booked for every night of the stay",
        "return transport booked",
        "local transport or transfers arranged",
        "online check-in (airlines open it about 24h before departure)",
        "travel documents valid for the destination",
        "calendar blocked for the dates",
    ],
    "purchase": [
        "order confirmed",
        "payment charged for the expected amount",
        "shipping notification received",
        "delivered",
        "item matches what was wanted (size, model, compatibility)",
        "return or warranty window noted",
    ],
    "work_item": [
        "issue reproduced",
        "root cause identified",
        "fix branch or pull request prepared",
        "reporter answered",
        "deployed or verified in production",
    ],
    "commitment": [
        "deadline known",
        "work scheduled",
        "delivered or answered",
    ],
    "check": [
        "inconsistency confirmed with the owner",
    ],
    "routine": [],
    "other": [],
}


# Template items are stable identifiers (the model reports status against them); these are their labels.
LABELS: dict[str, dict[str, str]] = {
    "es": {
        "outbound transport booked": "transporte de ida reservado",
        "lodging booked for every night of the stay": "alojamiento reservado para todas las noches",
        "return transport booked": "transporte de vuelta reservado",
        "local transport or transfers arranged": "transporte local o traslados resueltos",
        "online check-in (airlines open it about 24h before departure)": "check-in online (se abre unas 24 h antes de salir)",
        "travel documents valid for the destination": "documentación válida para el destino",
        "calendar blocked for the dates": "fechas bloqueadas en el calendario",
        "order confirmed": "pedido confirmado",
        "payment charged for the expected amount": "cobro por el importe esperado",
        "shipping notification received": "aviso de envío recibido",
        "delivered": "entregado",
        "item matches what was wanted (size, model, compatibility)": "el artículo coincide con lo buscado (talla, modelo, compatibilidad)",
        "return or warranty window noted": "plazo de devolución o garantía anotado",
        "issue reproduced": "incidencia reproducida",
        "root cause identified": "causa identificada",
        "fix branch or pull request prepared": "rama o pull request con la corrección",
        "reporter answered": "respuesta a quien lo reportó",
        "deployed or verified in production": "desplegado o verificado en producción",
        "deadline known": "plazo conocido",
        "work scheduled": "trabajo planificado",
        "delivered or answered": "entregado o respondido",
        "inconsistency confirmed with the owner": "incoherencia confirmada contigo",
    },
}


def label(item: str, language: str | None) -> str:
    """Human label for a checklist item in the owner's language; unknown items pass through."""
    table = LABELS.get(str(language or "en").split("-")[0].lower(), {})
    return table.get(" ".join(str(item or "").lower().split()), str(item or ""))


def template_for(kind: str) -> list[dict[str, str]]:
    return [{"item": item, "status": "unknown", "note": ""} for item in CHECKLISTS.get(kind, [])]


def merge_checklist(existing: list[dict], proposed: list[dict]) -> list[dict]:
    """Keep existing items, update statuses the model reports, append new items."""
    merged: list[dict] = []
    seen: set[str] = set()
    by_item = {str(p.get("item", "")).strip().lower(): p for p in proposed if isinstance(p, dict)}
    for row in existing:
        if not isinstance(row, dict):
            continue
        key = str(row.get("item", "")).strip().lower()
        update = by_item.get(key)
        entry = {"item": row.get("item", ""), "status": row.get("status", "unknown"), "note": row.get("note", "")}
        if update:
            if update.get("status") in {"done", "missing", "unknown", "not_applicable"}:
                entry["status"] = update["status"]
            if update.get("note"):
                entry["note"] = str(update["note"])[:300]
        merged.append(entry)
        seen.add(key)
    for key, row in by_item.items():
        if key and key not in seen:
            merged.append({
                "item": str(row.get("item", ""))[:200],
                "status": row.get("status") if row.get("status") in {"done", "missing", "unknown", "not_applicable"} else "unknown",
                "note": str(row.get("note", ""))[:300],
            })
    return merged[:30]
