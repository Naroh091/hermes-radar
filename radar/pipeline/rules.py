"""Deterministic triage rules. They run before any model call and are cheap by design."""
from __future__ import annotations

import re

TRANSACTIONAL = re.compile(
    r"\b(confirm\w*|order|pedido|compra|purchase|receipt|recibo|invoice|factura|booking|reserva|reservation|"
    r"itinerar\w*|boarding|embarque|ticket|billete|flight|vuelo|hotel|check[- ]?in|shipp\w*|env[ií]o|enviado|"
    r"deliver\w*|entrega\w*|tracking|seguimiento|payment|pago|charged|cobro|refund|reembolso|renewal|renovaci[oó]n|"
    r"deadline|plazo|vence\w*|due|expira\w*|appointment|cita|meeting|reuni[oó]n|invitation|invitaci[oó]n|"
    r"bug|error|incident\w*|outage|ca[ií]da|pull request|\bPR\b|issue|deploy\w*|release|security|seguridad|"
    r"password|contrase[nñ]a|verification|verificaci[oó]n|cancel\w*|change\w*|cambio|modificaci[oó]n|interview|entrevista)\b",
    re.IGNORECASE,
)
NOISE_SUBJECT = re.compile(
    r"(newsletter|bolet[ií]n|unsubscribe|digest|weekly|semanal|promo\w*|oferta|descuento|% ?off|sale\b|rebajas|"
    r"webinar|black friday|cyber monday|you may like|te puede interesar|novedades|new arrivals|follow(?:ed)? you|"
    r"te sigue|ha publicado|new post|comment\w* on|mentioned you|trending|recomendaci[oó]n)",
    re.IGNORECASE,
)
STRONG_AUTOMATION = {"noreply-sender", "precedence:bulk", "precedence:list", "auto-submitted:auto-generated"}
MIN_TURN_CHARS = 15
TASKY_TURN = re.compile(
    r"\b(busca\w*|compra\w*|reserva\w*|planific\w*|organiz\w*|recu[eé]rda\w*|avisa\w*|prepara\w*|env[ií]a\w*|"
    r"arregla\w*|revisa\w*|investiga\w*|necesito|quiero|tengo que|hay que|antes de|plazo|viaje|vuelo|hotel|"
    r"find|buy|book|plan|organi[sz]e|remind|prepare|send|fix|review|investigate|i need|we need|have to|must|"
    r"deadline|trip|flight|order|purchase|schedule)\b",
    re.IGNORECASE,
)


def subject_is_noise(subject: str) -> bool:
    return bool(NOISE_SUBJECT.search(subject or ""))


def is_transactional(text: str) -> bool:
    return bool(TRANSACTIONAL.search(text or ""))


def is_tasky_turn(text: str) -> bool:
    return bool(TASKY_TURN.search(text or ""))
