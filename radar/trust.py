"""Trust levels: which action classes may run without asking.

Classes come from :mod:`radar.reversibility`: ``read`` (look, analyse), ``local`` (reversible
and private: branch, draft PR, file, cron, draft reply) and ``external`` (irreversible or visible
to third parties: send, book, pay, deploy, merge, publish).
"""
from __future__ import annotations

from typing import Any

LEVELS = ("cautious", "balanced", "autonomous")
_AUTO_BY_LEVEL = {
    "cautious": frozenset({"read"}),
    "balanced": frozenset({"read", "local"}),
    "autonomous": frozenset({"read", "local"}),
}
NEVER_AUTO = frozenset({"payments.charge", "booking.reserve", "deploy.run", "github.merge_pr"})
ASK_FIRST = frozenset({"cron.create"})  # reversible, but a standing automation is always the owner's call


def describe(level: str) -> dict[str, Any]:
    classes = sorted(_AUTO_BY_LEVEL.get(level, _AUTO_BY_LEVEL["balanced"]))
    return {"level": level, "auto_classes": classes, "asks_for": "external"}


def may_auto_run(trust: dict[str, Any], action_class: str, *, kind: str | None = None, operation_id: str | None = None) -> bool:
    """True when ``action_class`` may run unattended under the configured trust."""
    level = str(trust.get("level") or "balanced")
    auto = set(_AUTO_BY_LEVEL.get(level, _AUTO_BY_LEVEL["balanced"]))
    overrides = trust.get("overrides") or {}
    if kind and isinstance(overrides.get(kind), dict):
        rule = overrides[kind].get(action_class)
        if rule == "auto":
            auto.add(action_class)
        elif rule == "ask":
            auto.discard(action_class)
    if operation_id and operation_id in ASK_FIRST:
        return False
    if action_class == "external":
        if operation_id and operation_id in NEVER_AUTO:
            return False
        allowed = trust.get("auto_external") or []
        return level == "autonomous" and bool(operation_id) and operation_id in allowed
    return action_class in auto


GUIDE: dict[str, dict[str, Any]] = {
    "es": {
        "intro": (
            "Cada paso que Radar propone tiene una clase según lo que cambia:\n"
            "  read      consultar, comparar, analizar, reproducir un bug. No cambia nada.\n"
            "  local     cambios reversibles y privados: borrador de respuesta, rama, PR en borrador, informe, recordatorio.\n"
            "  external  irreversible o visible para terceros: enviar un email, reservar, pagar, desplegar, mergear, publicar.\n"
            "El nivel de confianza decide qué clases corren solas y cuáles te preguntan antes."
        ),
        "levels": {
            "cautious": "cautious — Solo mira. Corre solo lo de clase read; cualquier cambio, aunque sea un borrador, te pregunta antes.",
            "balanced": "balanced — Mira y prepara. Corre solo read y local; todo lo externo te pregunta siempre. (Recomendado.)",
            "autonomous": "autonomous — Mira, prepara y actúa. Como balanced, y además ejecuta sin preguntar las operaciones externas que listes en trust.auto_external (p. ej. gmail.reply). Pagar, reservar, desplegar y mergear nunca corren solos.",
        },
        "examples": (
            "Ejemplo 1 — llega un email de un cliente con un bug de login en tu repo:\n"
            "  cautious   te avisa con lo que se ve en el email y pregunta «¿Investigo?». Solo tras tu sí reproduce el bug.\n"
            "  balanced   reproduce el bug, crea una rama y un PR en borrador, y te avisa con el resumen, el PR y un borrador de respuesta. Enviar la respuesta te lo pregunta.\n"
            "  autonomous con auto_external: [gmail.reply], además envía la respuesta al cliente. Mergear o desplegar sigue pidiendo confirmación.\n"
            "Ejemplo 2 — llega la confirmación de un vuelo y no hay hotel para esas noches:\n"
            "  cautious   avisa «falta alojamiento» y pregunta «¿busco opciones?».\n"
            "  balanced   busca opciones solo y te manda tres candidatos con precio. Reservar te lo pregunta.\n"
            "  autonomous igual que balanced: reservar y pagar nunca se automatizan."
        ),
        "overrides": "Afinar por tipo de tarea: trust.overrides: {work_item: {local: auto}} o {trip: {local: ask}}.",
    },
    "en": {
        "intro": (
            "Every step Radar proposes has a class, by what it changes:\n"
            "  read      look up, compare, analyse, reproduce a bug. Changes nothing.\n"
            "  local     reversible and private changes: a draft reply, a branch, a draft PR, a report, a reminder.\n"
            "  external  irreversible or visible to third parties: send an email, book, pay, deploy, merge, publish.\n"
            "The trust level decides which classes run on their own and which ask you first."
        ),
        "levels": {
            "cautious": "cautious — Looks only. Runs read steps alone; any change, even a draft, asks first.",
            "balanced": "balanced — Looks and prepares. Runs read and local alone; everything external always asks. (Recommended.)",
            "autonomous": "autonomous — Looks, prepares and acts. Like balanced, plus the external operations you list in trust.auto_external (e.g. gmail.reply) run without asking. Paying, booking, deploying and merging never run alone.",
        },
        "examples": (
            "Example 1 — a customer emails a login bug in your repo:\n"
            "  cautious   notifies you with what the email shows and asks “Shall I investigate?”. Only after your yes does it reproduce the bug.\n"
            "  balanced   reproduces the bug, creates a branch and a draft PR, and notifies you with the summary, the PR and a draft reply. Sending the reply asks you.\n"
            "  autonomous with auto_external: [gmail.reply], it also sends the reply. Merging or deploying still asks.\n"
            "Example 2 — a flight confirmation arrives and there is no hotel for those nights:\n"
            "  cautious   says “lodging missing” and asks “shall I look for options?”.\n"
            "  balanced   searches on its own and sends you three candidates with prices. Booking asks you.\n"
            "  autonomous same as balanced: booking and paying are never automated."
        ),
        "overrides": "Tune per task kind: trust.overrides: {work_item: {local: auto}} or {trip: {local: ask}}.",
    },
}


def _guide(language: str | None) -> dict[str, Any]:
    code = str(language or "es").lower().split("-")[0]
    return GUIDE.get(code, GUIDE["en"])


def level_summary(level: str, language: str | None = "es") -> str:
    return _guide(language)["levels"].get(level, _guide(language)["levels"]["balanced"])


def guide_text(language: str | None = "es") -> str:
    guide = _guide(language)
    return "\n".join([guide["intro"], "", *guide["levels"].values(), "", guide["examples"], "", guide["overrides"]])
