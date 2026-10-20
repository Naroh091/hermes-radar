# Radar

Plugin de proactividad para Hermes Agent. La versión 0.3 convierte correos, turnos de chat y eventos en **tareas de larga vida** (viajes, compras, incidencias, compromisos), las contrasta con los **hechos del propietario**, delega trabajo acotado a sesiones `hermes chat` y presenta **propuestas con botones** en Telegram. Las acciones externas (enviar, reservar, pagar, desplegar) siempre requieren consentimiento explícito.

El plugin es genérico: cuentas, canal, idioma y servidores MCP salen de la configuración; el onboarding descubre los MCP por la huella de sus herramientas.

## Cómo funciona

```
fuentes ──► signals ──► triaje (reglas) ──► extracción (LLM) ──► correlación ──► tasks
 gmail (MCP, push Pub/Sub o polling)                                  │
 sesiones (hooks post_llm_call / subagent_stop)                       ▼
 jobs terminados                              razonamiento: checklist por tipo + hechos + preferencias
                                                                      │
                                              proposals ──► outbox ──► Telegram (botones) / jobs (hermes chat)
```

- **Triaje sin modelo**: remitentes automáticos sin señal transaccional, boletines y turnos cortos se descartan antes de gastar tokens.
- **Un bundle, una extracción**: las señales se agrupan por hilo o sesión (con *debounce*) y se extraen en una sola llamada.
- **Clases de acción por reversibilidad**: `read` (buscar, analizar), `local` (borrador, rama, PR en borrador, informe) y `external` (irreversible o con terceros). El nivel de confianza decide qué corre solo: `cautious` solo mira, `balanced` mira y prepara, `autonomous` además ejecuta las operaciones externas que listes; pagar, reservar, desplegar y mergear nunca. Ejemplos en `docs/onboarding.md`.
- **Presupuestos diarios** de llamadas al modelo, jobs y avisos; horas de silencio; digest para lo no urgente.

## Instalación

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install .            # sin dependencias obligatorias: aiohttp, PTB y cryptography vienen del host
ln -s "$PWD" ~/.hermes/plugins/radar
```

Activa el plugin en `~/.hermes/config.yaml` (`plugins.enabled: [radar]`) y ejecuta el onboarding:

```bash
hermes radar discover      # clasifica los MCP del perfil (gmail-mcp, google-calendar-mcp...)
hermes radar setup         # confianza, propietario, fuentes, push de Gmail, ruta webhook
hermes radar validate
```

Después, desde el chat del propietario en Telegram: `/radar verify`. Guía completa en `docs/onboarding.md`.

## Configuración (resumen)

```yaml
plugins:
  entries:
    radar:
      mcp_allowlist: [gmail-work, google-calendar]
      settings:
        enabled: true
        mode: active
        owner: {id: me, timezone: Europe/Madrid, language: es}
        trust: {level: balanced, overrides: {}, auto_external: []}
        destination:
          platform: telegram
          address: {chat_id: "123456789"}
          owner_platform_id: "123456789"
        notifications: {enabled: true, quiet_start: "22:00", quiet_end: "08:00", daily_limit: 8, digest_times: ["09:00", "14:00", "19:00"]}
        observation: {local_sessions: true, debounce_seconds: 120, session_quiet_minutes: 30, session_auto_jobs: false}
        sources:
          - id: gmail-work
            kind: gmail
            address: user@example.org
            provider: gmail_mcp
            server: gmail-work
            labels: [INBOX]
            interval_seconds: 300          # polling (por defecto); con push sigue como red de seguridad
            push: {enabled: false, topic: projects/x/topics/gmail, credentials_path: ~/.hermes/integrations/gmail-work}  # opcional
          - id: google-calendar
            kind: calendar
            provider: gcal_mcp
            server: google-calendar
            calendar_id: primary
            interval_seconds: 600         # ventana [ahora-24h, ahora+30d]; huella por evento
            lookahead_days: 30
            initial_import: false         # true: los eventos ya existentes también generan señales en la primera lectura
        notices_per_day: 8             # avisos automáticos al día (formulario de Desktop; gana sobre notifications.daily_limit)
        quiet_start: "22:00"           # horas de silencio, también en el formulario
        quiet_end: "08:00"
        jobs_per_day: 100              # también en el formulario de Desktop (gana sobre limits.jobs_per_day)
        auto_jobs_per_task: 3          # jobs automáticos por tarea y día; lo que autorizas tú no cuenta
        auto_job_cooldown_minutes: 15
        jobs: {enabled: true, max_concurrent: 2, max_turns: 20, max_seconds: 300}
        workspaces: {"org/repo": /home/me/src/repo}
```

Los secretos nunca van en YAML: `RADAR_GMAIL_PUSH_TOKEN` y `RADAR_GMAIL_PUSH_HERMES_SECRET` viven en `~/.hermes/.env` (los escribe `setup`).

Modelos por paso: el plugin registra las tareas auxiliares `radar_extract`, `radar_plan`, `radar_intent`, `radar_pattern` y `radar_card`; fíjalas en `auxiliary.radar_<paso>` para usar un modelo barato.

## Herramientas, comandos y CLI

- Tools: `radar_status`, `radar_tasks`, `radar_resolve`, `radar_explain`, `radar_preference` (+ `radar_list`, `radar_inspect`, `radar_propose` del monitor HTTPS).
- Comando en sesión: `/radar verify | status | list | prefs | forget <id> | patterns | pause | resume | mute <id> | trust <nivel>` (`/radar` ya es un alias de `/loop` en Hermes).
- CLI: `hermes radar {status|validate|run-once|discover|setup|sync <fuente>|replay <señal>|tasks|explain <tarea> [--html]|signals|gmail watch|gmail receiver|github route [nombre]|import mail-intelligence}`.

## Seguridad

- Scope `(profile_key, owner_id)` en todas las tablas; configuración cerrada a claves desconocidas.
- El contenido de correos y sesiones es **datos, nunca instrucciones** en todos los prompts.
- Retención: snippets acotados por defecto; el cuerpo completo solo si la tarea lo necesita y con caducidad (`retention.full_content_days`).
- Los jobs son sesiones `hermes chat` con presupuesto de turnos y tiempo; el prompt limita los efectos a la clase de acción. Una acción externa nunca arranca sola: llega como propuesta con botones y, cuando la autorizas, se lanza un job que lleva la autorización exacta (paso, opción elegida, hora) y solo puede hacer eso. La respuesta al botón dice si el job está en marcha o por qué todavía no (`Autorizado: … En marcha` / `Todavía no se ha ejecutado: …`). Lo autorizado que no puede arrancar queda en estado `waiting` (visible en `/radar status`, `/radar list` y `hermes radar tasks`) y el scheduler lo reintenta en cada tick; «No» lo cancela.
- Freno a los bucles: por tarea, como máximo `auto_jobs_per_task` jobs automáticos al día y `auto_job_cooldown_minutes` entre uno y otro (tu autorización explícita no está limitada, salvo por `jobs_per_day`, 100 por defecto). Un paso que descartas no se vuelve a proponer, ni reformulado, durante 24 h.
- GitHub: fuente por dos vías que se combinan. El webhook del host (`hermes webhook`): Radar observa en `pre_gateway_dispatch` la ruta que indiques (`route`), por ejemplo la que ya usa otro perfil como gatekeeper de PRs, sin interferir (`consume: false`), o una ruta propia instalada con `hermes radar github route` que consume sin turno de agente. Y el sondeo con `gh` (`repos`): la API de eventos con ETag cada `interval_seconds`, primera lectura como línea base. Filtros por defecto: PRs abiertos, listos, cerrados o con revisión solicitada; issues abiertos, cerrados o asignados; pushes a la rama por defecto (`all_branches` para todas); workflows fallidos; resultados de despliegue; releases publicadas; revisiones que aprueban o piden cambios; comentarios. Los bots se descartan salvo runs y despliegues.
- Sesiones activas: un turno de chat significa que el agente ya está en ello dentro de esa conversación. Radar no procesa el lote hasta que la sesión lleva `observation.session_quiet_minutes` (30) en silencio; la tarea que nace de una sesión recuerda su id y, mientras esa sesión siga viva, no genera avisos, no lanza jobs en paralelo y pospone sus revisiones. Tampoco se redacta ninguna «respuesta» para tareas sin interlocutor (sin hilo de correo ni issue).
- Sesiones juzgadas en conjunto: un turno nunca se evalúa solo. Cuando la sesión calla, los turnos pendientes viajan al modelo con el resto de la transcripción (los ya evaluados como contexto) y el modelo decide dónde terminó la conversación: `resolved` (todo lo pedido quedó respondido o hecho; es ruido, aunque fuera largo o importante, y cierra en silencio la tarea que esa misma sesión abrió), `open` (queda algo por hacer, y quién) o `deferred` (se dejó para una fecha, con recordatorio o cron). Una pregunta respondida, una decisión ya tomada o un trabajo que el agente completó no generan tarea. Un lote de conversación solo se correlaciona con tareas nacidas de la misma sesión o que nombren el mismo identificador (PR, pedido, evento), nunca por compartir tema. De lo que empezó en un chat Radar nunca lanza jobs por su cuenta (`observation.session_auto_jobs: false`): lo que quede pendiente se ofrece en el resumen y lo decides tú. Las credenciales pegadas en un chat (JWT, tokens de Meta, GitHub, Slack, claves de API, «Bearer …», `password: …`) se enmascaran antes de guardarse y antes de llegar a cualquier modelo.
- Urgencia antes que aviso: cada plan juzga `urgency` (`now`, `today`, `this_week`, `whenever`) e `importance`. Solo `now` interrumpe (alerta de seguridad, pago bloqueado, plazo en horas, alguien esperando hoy, servicio caído); el resto se agrupa y espera su franja de resumen (`notifications.digest_times`, hora local, por defecto 09:00, 14:00 y 19:00): `today` toma la siguiente franja del día, `this_week` y `whenever` la primera franja de la mañana. Lo que ya sabes (un correo que escribiste tú, lo que tu chat hizo o decidió) no se te cuenta. Las horas de silencio solo las cruza un aviso `now` con un paso de urgencia alta. Con `digest_times: []` el resumen vuelve a la cadencia de `batch_minutes`.
- Escalado: cada plan decide quién actúa a continuación. `handle`: el asistente puede averiguarlo o resolverlo solo con pasos de lectura (abrir la notificación de DEHú, leer el mensaje, comprobar si los pagos de Payoneer han llegado); el paso se ejecuta sin avisar y el resultado se vuelve a planificar. `inform`: hay un resultado que contar pero nada que decidir («Enhorabuena, has cobrado 1.500 € 🤑»); llega un aviso con la noticia y un solo botón «Vale», sin preguntas, y como mucho una oferta barata ligada a una tarea (marcar la factura como cobrada o la devolución como terminada con `task.update`, que siempre pregunta). `ask`: solo cuando te toca decidir, hay riesgo o una acción irreversible. Nunca se te pide que vayas a leer o comprobar algo que el asistente puede leer o comprobar él mismo.
- Patrones (F3): una rutina diaria mina el log de señales de las últimas 10 semanas: periódicos (el mismo actor hace lo mismo el mismo día y franja horaria, soporte ≥ 3 en ≥ 3 semanas, concentración ≥ 60 %) y secuencias (B sigue a A en menos de 7 días con lift ≥ 2). El modelo nombra cada candidato y decide si merece una automatización que solo prepare o informe; si sí, aparece como tarea `routine` con la propuesta «Automatizar» (siempre pregunta, nunca corre sola). Aceptar crea un cron del host (`cron.create`, nativo, verificado) que entrega en tu chat; sus salidas vuelven como señales `cron_result` y avanzan la rutina. `/radar patterns` las lista; descartar una la silencia por huella.
- Tarjetas-objeto: en el panel cada tarea se dibuja como el objeto de su mundo y la forma lleva datos reales: viaje = tarjeta de embarque (ruta `MAD → LIS`, fechas, localizador, talón perforado), compra = ticket de caja (líneas, importe, pedido, entrega), trabajo = ticket de PR (repo, rama, número, raíl de diff), compromiso = hoja de calendario (día en grande, cuenta atrás), comprobación = etiqueta de precaución, rutina = esfera con el horario. Fondos y texto son los del tema de Hermes; cada tipo solo aporta un tono. Tipos nuevos: una rutina diaria agrupa las tareas «otra» repetidas (≥ 3 parecidas) y pide al modelo un tipo con nombre, icono, tono, plantilla de objeto y campos a destacar (`card_kinds`, slot `card`); el panel lo pinta desde entonces y se puede desactivar desde el propio panel.
- Panel Desktop (F4): paquete unificado. `dashboard/plugin_api.py` monta `/api/plugins/radar/` (tablero, detalle e informe de cada tarea, responder propuestas con las mismas opciones que los botones, acciones de tarea, preferencias, patrones, socket `/events`) y `desktop/plugin.js` añade la página «Radar» a Hermes Desktop con la lista de tareas por estado, el detalle con contexto del hilo, propuestas y borradores, el informe HTML embebido en un marco aislado, patrones y preferencias. Las decisiones tomadas en el panel las ejecuta el gateway en su siguiente tick.
- Flujo work_item de punta a punta: un correo con un bug en un repo de `workspaces` lanza (bajo `balanced`) un job en un worktree aislado de ese checkout; el job reproduce, arregla en una rama nueva, la sube y devuelve su nombre (`branch`, `repo`) y los hallazgos; Radar recuerda la rama en la tarea (`entities.branch`) y propone abrir el PR en borrador, que ejecuta de forma nativa (`github.open_pr`: siempre draft, con marca de origen `hermes-radar` y la tarea, sin auto-merge, verificado releyendo) y recuerda su URL (`entities.pr`) para no repetirlo; el aviso del resultado lleva el informe HTML de la tarea adjunto y, si hay respuesta redactada, el borrador para enviarla.
- Operaciones nativas con consentimiento: `gmail.reply` (envía el borrador aprobado, por id de borrador o como respuesta al último mensaje del hilo), `calendar.respond` (acepta, declina o tentativa) y `github.comment` (publica el texto aprobado en el PR o issue) y `github.open_pr` (PR en borrador desde la rama recordada) las ejecuta el propio plugin, sin agente, como jobs de tipo `native`. Al pulsar el botón se registra un consentimiento HMAC de un solo uso ligado a la operación y a sus parámetros exactos (hash); el job lo consume justo antes de actuar, actúa una vez y verifica releyendo (mensaje enviado, respuesta en el evento, comentario publicado). Si no puede verificar, la propuesta queda en `unknown` y el aviso lo dice («hecho, pero sin poder verificarlo»); nunca se reintenta a ciegas. Lo que no sea nativo o no tenga parámetros resolubles sigue en un job delegado.
- Calendario: con el MCP de Google Calendar (`gcal_mcp`, familia @cocal) Radar lee la ventana de eventos cada `interval_seconds` y guarda una huella por evento (hora, título, lugar, estado, descripción, organizador y tu respuesta). Solo los cambios generan señales: `created`, `updated` y `cancelled` (también cuando un evento futuro desaparece). La primera lectura solo toma la línea base; las invitaciones sin responder llegan al modelo como decisión tuya y los eventos que declinaste no se siguen. Responder a invitaciones es una acción externa y requiere consentimiento.
- Memoria y sesiones: antes de planificar, Radar consulta el proveedor de memoria activo del host a través de su propia herramienta de búsqueda (`hindsight_recall`, `mnemosyne_recall`, la `*_query` de otro proveedor; `context.memory_tool` fija una) y la búsqueda de sesiones anteriores (`session_search`). Lo recordado entra en el prompt como datos con límite de tamaño y tiempo (`context.max_chars`, `context.timeout_seconds`); si el proveedor no responde, se planifica sin él.
- Preferencias: responder a un aviso con «no me avises más de este remitente», «los de CI solo en el resumen» o «avísame siempre de esto» guarda una preferencia con alcance (remitente, cuenta, tipo de tarea, fuente), que el triaje y la atención aplican desde la siguiente señal. `/radar prefs` las lista, `/radar forget <id>` desactiva una; el agente puede guardarlas con `radar_preference`.
- Contexto del hilo: si la tarea nace de un correo, el aviso incluye quién escribió el último mensaje, cuándo (en tu zona horaria), el asunto, cuántos mensajes lleva el hilo y un extracto del último sin el historial citado; el resumen diario lleva solo la cabecera y «Revisarlo primero» cita el mensaje al que se responde.
- Borradores: cuando un paso prepara una respuesta, el job la guarda como borrador en Gmail y devuelve el texto; el aviso lo muestra entero («Propongo responder») con los botones «Enviar borrador / Revisarlo primero», y el job de envío usa exactamente ese texto. Solo el primer botón ejecuta; «Revisarlo primero» (o cualquier opción intermedia) manda el texto completo en un mensaje aparte y deja la propuesta abierta; responder «sí» a ese mensaje la aprueba.
- `notices_per_day` limita los avisos automáticos; lo que responde a una autorización tuya (resultado de un job que aprobaste) se entrega siempre, aunque el cupo esté agotado.
- Los textos se piden a medida (título < 80, justificación < 300, etiquetas de botón < 32 caracteres) y, si aun así el aviso supera el límite de Telegram, se acortan por palabras enteras, nunca a mitad de frase.
- Los botones de Telegram solo responden al actor y chat verificados; las respuestas en texto se clasifican con negaciones por delante y nunca aprueban acciones externas.

## Verificación

```bash
uv run --with pytest python -m pytest -q      # 73 tests
python3 -m compileall -q radar tests
hermes plugins doctor . --ci
```
