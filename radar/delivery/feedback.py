"""Free-text intent cues for replies to a notice (ported from mail-intelligence, es + en).

A regex alone never approves an external action: negations win, and the callback buttons remain the
unambiguous path. The model may refine the intent; code validates the answer and keeps the floor.
"""
from __future__ import annotations

import json
import re
from typing import Any

_EDIT = re.compile(r"m[áa]s breve|m[áa]s corto|cambia|corrige|reescribe|edita|shorter|rewrite|change it|edit")
_PREF = re.compile(r"no me avises|no avisar|siempre|todos los|estas alertas|nunca me|don'?t notify|stop notifying|always|never notify|mute these")
_LATER = re.compile(r"m[áa]s tarde|luego|ma[ñn]ana|recu[ée]rdamelo|later|tomorrow|remind me|not now|ahora no")
_REJECT = re.compile(r"\bdescarta\b|\bcancela\b|rechazo|ignora|no lo env|no la env|no env|\bdismiss\b|\bcancel\b|\breject\b|don'?t send|do not send|\bskip\b")
_APPROVE = re.compile(r"\bs[íi]\b|env[íi]a|adelante|apruebo|\bvale\b|\bok\b|de acuerdo|hazlo|\byes\b|\bgo ahead\b|\bapprove\w*\b|\bdo it\b|\bsend it\b|\bsure\b")
_QUESTION = re.compile(r"\?|^(qu[ée]|c[óo]mo|cu[áa]l|has |has comprobado|por qu|what|how|which|why|did you)")
_THANKS = re.compile(r"gracias|thanks|thank you")
_NEGATION = re.compile(
    r"\bno\b|\bnunca\b|\bjam[áa]s\b|\bpara nada\b|\btodav[íi]a no\b|\ba[úu]n no\b|\bespera\b|\besp[ée]rate\b|\bmejor no\b"
    r"|\bnot\b|\bnever\b|\bdon'?t\b|\bdo not\b|\bwait\b|\bhold on\b"
)
_DONE = re.compile(r"ya est[áa]|ya lo (he )?hecho|hecho ya|already done|done already|i did it|ya lo compr|ya lo reserv|already (booked|bought|sent)")

INTENTS = ("approve", "reject", "edit_request", "preference", "question", "thanks", "later", "done", "unclear")

INTENT_SYSTEM = """Classify one free reply the owner sent to a notification with proposals.
The reply can approve, reject, ask for a later reminder (later), say the thing is already done (done),
request an edit, state a preference, ask a question, thank, or be unclear. Approving requires an explicit
instruction to carry out the proposed action. A question, a greeting, a thank-you or an edit request never
approves. A negation ("no", "nunca", "todavía no", "not", "don't") before an approval cue makes the reply
a rejection or unclear, never approval. proposal_index is the 1-based number the owner refers to, or 0.
Reply only with JSON matching the schema."""


def _negated_approval(lowered: str) -> bool:
    for clause in re.split(r"[.;!?\n]", lowered):
        if _APPROVE.search(clause) and _NEGATION.search(clause):
            return True
    return False


def has_negation(lowered: str) -> bool:
    return bool(_NEGATION.search(lowered))


def classify(text: str) -> dict[str, Any]:
    """Deterministic floor. Never returns ``approve`` for a negated reply."""
    lowered = " ".join(str(text or "").split()).lower()
    if not lowered:
        return {"intent": "unclear"}
    index = 0
    number = re.match(r"^\s*(\d)\b", lowered)
    if number:
        index = int(number.group(1))
    if _DONE.search(lowered):
        return {"intent": "done", "proposal_index": index}
    if _PREF.search(lowered):
        return {"intent": "preference", "proposal_index": index}
    if _LATER.search(lowered) and not _APPROVE.search(lowered):
        return {"intent": "later", "proposal_index": index}
    if _EDIT.search(lowered):
        return {"intent": "edit_request", "proposal_index": index}
    if _REJECT.search(lowered) or _negated_approval(lowered):
        return {"intent": "reject" if _REJECT.search(lowered) else "unclear", "proposal_index": index}
    if _THANKS.search(lowered) and "?" not in lowered:
        return {"intent": "thanks", "proposal_index": index}
    if _QUESTION.search(lowered):
        return {"intent": "question", "proposal_index": index}
    if _APPROVE.search(lowered) or (number and len(lowered) <= 3):
        return {"intent": "approve", "proposal_index": index}
    return {"intent": "unclear", "proposal_index": index}


def validate_intent(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("intent") not in INTENTS:
        raise ValueError("invalid intent")
    index = raw.get("proposal_index", 0)
    return {"intent": raw["intent"], "proposal_index": int(index) if isinstance(index, int) else 0, "reason": str(raw.get("reason") or "")[:200]}


def authorizes_execution(intent: Any) -> bool:
    value = intent.get("intent") if isinstance(intent, dict) else intent
    return value == "approve"
