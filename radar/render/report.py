"""Standalone HTML report for one task: no external assets, no scripts."""
from __future__ import annotations

import html
from typing import Any

from ..i18n import t

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#5d6675;--accent:#2f6feb;--ok:#1f9d55;--warn:#d97706;--bad:#dc2626;--line:#e5e8ee}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b24;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a3140}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
main{max-width:860px;margin:0 auto}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:14px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:15px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:0 0 10px}
.meta{color:var(--muted);font-size:13px}ul{padding-left:18px;margin:6px 0}li{margin:3px 0}
.status{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:var(--line)}
.done{color:var(--ok)}.missing{color:var(--bad)}.unknown{color:var(--muted)}.not_applicable{color:var(--muted);text-decoration:line-through}
.signal{border-left:3px solid var(--accent);padding-left:10px;margin:8px 0}.signal small{color:var(--muted)}
pre{white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:8px;font-size:13px}
"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def render_task_report(
    task: dict[str, Any], *, signals: list[dict[str, Any]], proposals: list[dict[str, Any]], facts: list[dict[str, Any]],
    jobs: list[dict[str, Any]] | None = None, language: str = "es",
) -> str:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_e(task.get('title'))}</title><style>{_CSS}</style></head><body><main>",
        "<div class='card'>",
        f"<h1>{_e(task.get('title'))}</h1>",
        f"<div class='meta'>{_e(t(language, 'task_label'))} · {_e(task.get('kind'))} · <span class='status'>{_e(task.get('status'))}</span>"
        + (f" · {_e(t(language, 'due'))}: {_e(str(task.get('due_at'))[:16])}" if task.get("due_at") else "") + "</div>",
        f"<p>{_e(task.get('summary'))}</p>",
        "</div>",
    ]
    checklist = [c for c in task.get("checklist") or [] if isinstance(c, dict)]
    if checklist:
        parts.append(f"<div class='card'><h2>{_e(t(language, 'checklist'))}</h2><ul>")
        for item in checklist:
            parts.append(f"<li class='{_e(item.get('status'))}'>{_e(item.get('item'))}"
                         + (f" <small>— {_e(item.get('note'))}</small>" if item.get("note") else "") + "</li>")
        parts.append("</ul></div>")
    if proposals:
        parts.append(f"<div class='card'><h2>{_e(t(language, 'proposals'))}</h2><ul>")
        for proposal in proposals:
            parts.append(f"<li><b>{_e(proposal.get('title'))}</b> <span class='status'>{_e(proposal.get('status'))}</span>"
                         + (f"<br><small>{_e(proposal.get('reason_text'))}</small>" if proposal.get("reason_text") else "") + "</li>")
        parts.append("</ul></div>")
    if jobs:
        parts.append("<div class='card'><h2>Jobs</h2><ul>")
        for job in jobs:
            parts.append(f"<li><span class='status'>{_e(job.get('status'))}</span> {_e(job.get('kind'))}"
                         + (f"<pre>{_e(job.get('result_summary'))}</pre>" if job.get("result_summary") else "") + "</li>")
        parts.append("</ul></div>")
    if signals:
        parts.append(f"<div class='card'><h2>{_e(t(language, 'findings'))}</h2>")
        for signal in signals:
            meta = signal.get("meta") or {}
            parts.append(f"<div class='signal'><b>{_e(signal.get('title'))}</b><br><small>{_e(signal.get('kind'))} · {_e(str(signal.get('occurred_at'))[:16])}"
                         + (f" · {_e(meta.get('sender'))}" if meta.get("sender") else "") + f" · {_e(signal.get('role', ''))}</small>"
                         + (f"<p>{_e(signal.get('snippet'))}</p>" if signal.get("snippet") else "") + "</div>")
        parts.append("</div>")
    if facts:
        parts.append("<div class='card'><h2>Facts</h2><ul>")
        for fact in facts[:20]:
            parts.append(f"<li>{_e(fact.get('key'))}: {_e(fact.get('value'))}</li>")
        parts.append("</ul></div>")
    parts.append(f"<p class='meta'>{_e(t(language, 'report_title'))} · {_e(task.get('id'))}</p></main></body></html>")
    return "".join(parts)
