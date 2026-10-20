"""Checklist templates per task kind. The model fills status per item; code never invents facts."""
from __future__ import annotations

import re
import unicodedata

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


_STOPWORDS = {"de", "del", "la", "el", "los", "las", "para", "en", "o", "y", "a", "al", "un", "una", "con", "cada", "todas", "todos", "the",
              "for", "of", "or", "with", "every", "it", "about", "before", "unas", "antes", "se", "abre", "lo", "que"}
_SYNONYMS = {"facturacion": "check-in", "facturar": "check-in", "embarque": "check-in", "checkin": "check-in"}


def _words(text: str) -> set[str]:
    """Accent-free, parenthesis-free significant word stems, so a translated or reworded label still matches its item."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    plain = re.sub(r"\([^)]*\)", " ", plain.lower())
    out: set[str] = set()
    for word in re.findall(r"[a-z0-9-]+", plain):
        word = _SYNONYMS.get(word, word)
        if word in _STOPWORDS or len(word) < 3:
            continue
        out.add(word[:5])
    return out


def canonical_item(text: str, template: list[str]) -> str:
    """The template item ``text`` stands for, when the model reworded or translated it; otherwise ``text`` itself."""
    key = " ".join(str(text or "").lower().split())
    if not key or key in template:
        return key
    tables = [table for table in LABELS.values()]
    for item in template:
        if any(" ".join(table.get(item, "").lower().split()) == key for table in tables):
            return item
    words = _words(key)
    if not words:
        return key
    best, best_score, runner_up = "", 0.0, 0.0
    for item in template:
        variants = [item] + [table[item] for table in tables if table.get(item)]
        score = max((len(words & _words(v)) / len(words | _words(v)) for v in variants if _words(v)), default=0.0)
        if score > best_score:
            best, best_score, runner_up = item, score, best_score
        elif score > runner_up:
            runner_up = score
    return best if best_score >= 0.5 and best_score > runner_up else key


def merge_checklist(existing: list[dict], proposed: list[dict]) -> list[dict]:
    """Keep existing items, update statuses the model reports, append new items.

    The model may reword or translate a template item; it is matched back to the item it stands for.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    template = [str(row.get("item", "")).strip().lower() for row in existing if isinstance(row, dict)]
    by_item: dict[str, dict] = {}
    for p in proposed:
        if isinstance(p, dict):
            by_item.setdefault(canonical_item(p.get("item", ""), template), p)
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
