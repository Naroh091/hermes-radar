from __future__ import annotations

import json
from typing import Any

STATUS = {"name": "proactive_status", "description": "Estado local del plugin proactivo; no aprueba operaciones.", "parameters": {"type": "object", "additionalProperties": False, "properties": {}}}
LIST = {"name": "proactive_list", "description": "Lista seguimientos del alcance actual.", "parameters": {"type": "object", "additionalProperties": False, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}}}
INSPECT = {"name": "proactive_inspect", "description": "Inspecciona un objeto local por ID; no ejecuta acciones.", "parameters": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {"id": {"type": "string"}}}}
PROPOSE = {"name": "proactive_propose", "description": "Registra una propuesta validada; nunca concede consentimiento ni ejecuta cambios.", "parameters": {"type": "object", "additionalProperties": False, "required": ["operation_id"], "properties": {"operation_id": {"type": "string"}, "parameters": {"type": "object"}, "followup_id": {"type": "string"}}}}


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
