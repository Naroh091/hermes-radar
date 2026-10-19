# Radar — compatibilidad 0.3.0

## Host de referencia

- Checkout: `/home/naroh/.hermes/hermes-agent`, Hermes v0.22.0 (`e06df9c2eae6`), runtime Python 3.11 (`venv/`).
- APIs públicas usadas: `register(ctx)`, `ctx.register_tool`, `ctx.register_hook` (`on_session_start`, `post_llm_call`, `subagent_stop`, `pre_gateway_dispatch`, `pre_llm_call`), `ctx.register_command`, `ctx.register_platform_handler("telegram")` (PTB `Application`: `CallbackQueryHandler(pattern=r"^p[at]:")`; los comandos van por `register_command` porque el gateway responde a los comandos que no están en su registro), `ctx.register_cli_command`, `ctx.register_auxiliary_task`, `ctx.llm.complete_structured` / `acomplete_structured`, `ctx.call_mcp` (bajo `mcp_allowlist`), `ctx.set_config`, `ctx.spawn_task`, `ctx.get_config`, `ctx.profile_name`, `ctx.on_unload`, `hermes_constants.get_hermes_home`.
- Lecturas opcionales de internals, siempre defensivas: `hermes_state.SessionDB(read_only=True)` (poller de sesiones, desactivado por defecto), `tools.mcp_schema_cache` vía el fichero `cache/mcp_schema_cache.json` (descubrimiento), `hermes_cli.config.load_config/save_config` (solo en `setup` para `mcp_allowlist`).
- Contratos del gateway en los que se apoya: los eventos de la plataforma `webhook` llegan a `pre_gateway_dispatch` con `source.user_id == "webhook:<ruta>"` y `raw_message` = payload; `{"action": "skip"}` evita el turno. `MessageEvent.reply_to_message_id` identifica respuestas a avisos.
- CLI pública usada por los jobs: `hermes chat --query-file - --oneshot --format stream-json --max-turns N --run-budget S [--in DIR] [--worktree] [-t ...] [-s ...] [-m ...]`.

## Evidencia ejecutada

- `uv run --with pytest python -m pytest -q`: **73 passed**.
- `python3 -m compileall -q radar tests`: pasa.
- `hermes plugins doctor . --ci`: `radar 0.3.1 (standalone)`, 7 herramientas y 5 hooks registrados.
- Importación bajo el venv del gateway (3.11) de `radar.runtime`, `radar.ingress.gmail_push_receiver` (app aiohttp con 5 rutas), `radar.cli`.
- Descubrimiento real sobre el perfil `default`: `gmail-maldita`, `gmail-naroh091`, `gmail-yo-naroh` ⇒ `gmail_mcp`; `google-calendar` ⇒ `gcal_mcp`.
- `validate` sobre la configuración viva del perfil (`enabled: true`, `mode: active`): válida tras normalizar `quiet_start`/`quiet_end` sexagesimales de YAML.
- Migración 006 ⇒ 009 sobre una copia de la base viva: ver sección siguiente.

- 19/10: receptor Pub/Sub dentro del gateway (`ingress.gmail_receiver.enabled`) escuchando en 127.0.0.1:8666 tras el arranque de Telegram; `routine:gmail-watch:<fuente>` renovó el watch de las tres cuentas en los dos primeros ticks (expiración a 7 días) con las credenciales del MCP; `session_search` del host responde en <1 s desde el plugin.

- 19/10 22:29: recuerdo en producción: `recall for <señal>: memory=1499 chars via hindsight/hindsight_recall, sessions=3` (proveedor cargado con `load_memory_provider` dentro del scope de secretos del perfil; `session_search` en proceso). La CLI `hermes radar sync` no puede usar MCP (cliente no conectado fuera del gateway).

## Contratos simulados en tests

- Store por hilo: tres hilos escribiendo señales a la vez sin errores; scope aislado también en `tasks`, `facts`, `tasks_fts`.
- Bundles por `group_key` con lease y fencing; deferral por presupuesto agotado; retención del contenido completo según `keep_full_content`.
- Pipeline completo con modelo simulado: extracción ⇒ tarea `trip` con checklist, hechos (`shoe size=42`), dos propuestas (una programada para T-24h), timer de revisión; un bundle de cierre pasa la tarea a `done` y cancela el timer.
- Auto-ejecución bajo `balanced`: la propuesta `read` se acepta sola y encola un job; la tarea pasa a `in_progress`.
- Jobs: `hermes chat` simulado (éxito, timeout, salida vacía); el resultado vuelve como señal `job_result` y cierra la propuesta.
- Entrega: un aviso por tarea con botones (`pa:<id>:<opción>`, `pt:<tarea>:later|mute`), digest, límite diario, horas de silencio que retienen lo no urgente, edición in-place y fallback a texto plano cuando Telegram rechaza HTML.
- Respuestas: botones, texto con negaciones ("adelante, pero no lo mandes" nunca aprueba), referencia numérica ("la 2"), snooze, mute, checks de consistencia.
- Memoria del host: `plugins.memory.load_memory_provider(name, register_skills=False)` + `initialize(session_id, hermes_home=, platform=, agent_context="subagent")`; búsqueda por `get_tool_schemas()`/`handle_tool_call()` (hindsight `hindsight_recall`, mnemosyne `mnemosyne_recall`, herramientas `*_query` con parámetro `question`), `queue_prefetch`/`prefetch` como respaldo; `shutdown()` al descargar. Sesiones: `tools.session_search_tool.session_search(query=, limit=)` con sobre `{success, results[{session_id, when, title, snippet}]}` (o `ctx.dispatch_tool("session_search", …)`). Todo acotado por tiempo con `asyncio.to_thread` + `wait_for`.
- GitHub: `MessageEvent.raw_message` del host lleva el payload del webhook y `source.user_id = webhook:<ruta>`; el tipo de evento se infiere del payload (sin la cabecera `X-GitHub-Event`). Sondeo con `gh api -i repos/{repo}/events` + `If-None-Match` (304 → sin cambios; `X-Poll-Interval: 60`). Escrituras con `gh api -X POST … --input -`.
- Cron del host: `cron.jobs.create_job(prompt, schedule, name, origin={platform, chat_id[, thread_id]}, deliver="origin")`, `get_job`, `get_cron_output_dir()`; salidas en `<cron>/output/<job>/<ts>.md` con `## Response` (`[SILENT]` = nada que contar).
- Panel: `dashboard/manifest.json` + `plugin_api.py` (`router = APIRouter()`, montado en `/api/plugins/radar/` por el dashboard/serve; WS autorizado con `web_server_chat._ws_auth_ok`), `desktop/plugin.js` (`@hermes/plugin-sdk`: `ROUTES_AREA`, `SIDEBAR_NAV_AREA`, `SandboxedFrame`, `useQuery`/`queryClient`, `ctx.rest`/`ctx.socket`).
- Draft PRs: `gh pr create -R repo --draft --head rama --base <default> --body-file -`, `gh pr list --head rama --state all --json …` para verificar e idempotencia; `validate_no_automerge` y `ensure_origin_mark` de `reversibility.py`.
- Operaciones nativas: `consents` reutilizada sin el join a `plans` (`record_native_consent`), `consume_consent` de un solo uso; jobs de `kind=native` por el mismo bucle que los delegados (`claim_queued_jobs` → `Runtime._run_job`); resultado por `record_job_outcome` (status `unknown` ⇒ propuesta `unknown`).
- Preferencias aprendidas: respuesta `preference` ⇒ modelo (slot `radar.preference`) o respaldo determinista ⇒ `store.save_preference` versionado ⇒ adaptadores de fuente refrescados.
- Fuentes: Gmail por MCP con triaje de boletines, lectura del cuerpo solo para lo que pasa el triaje, cursor por epoch; turnos de sesión con debounce y exclusión de `cron`/`webhook`; poller de `state.db` con cursor por sesión; receptor Pub/Sub (decodificación, dedupe, reenvío firmado HMAC V2 con `X-Request-ID`).

## Pendiente externo

1. Onboarding interactivo en la máquina del propietario (`hermes radar setup`): direcciones, chat de Telegram, topic Pub/Sub y credenciales no se pueden rellenar sin él.
2. Entrega real de un aviso con botones y un callback en la cuenta de Telegram verificada (el envío de texto ya estaba verificado en 0.2.0 con `platform_message_id=10892`).
3. Un job real de `hermes chat` desde el gateway con el workspace configurado.
4. F2: GitHub/Calendar como fuentes, catálogo de operaciones con consentimiento para `external`; F3 patrones; F4 panel Desktop.
