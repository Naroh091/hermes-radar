# Onboarding de Radar 0.3

## 1. Activar el plugin

```yaml
plugins:
  enabled: [radar]          # y quita mail-intelligence si lo tenías
  entries:
    radar:
      mcp_allowlist: [gmail-work, google-calendar]   # lo escribe `hermes radar setup`
```

Reinicia el gateway (`hermes gateway restart`). El scheduler arranca desde el handler de Telegram o en el primer `on_session_start` del gateway.

## 2. Descubrir y configurar

```bash
hermes radar discover
hermes radar setup
```

`setup` pregunta: idioma, nivel de confianza, propietario, zona horaria, horas de silencio, chat y usuario de Telegram, qué servidores Gmail/Calendar vigilar y la dirección de cada cuenta. Escribe `plugins.entries.radar.settings` y el `mcp_allowlist`.

### Niveles de confianza

Cada paso que Radar propone tiene una **clase** según lo que cambia:

| Clase | Qué es | Ejemplos |
|---|---|---|
| `read` | No cambia nada | buscar hoteles, comparar precios, leer el código, reproducir un bug |
| `local` | Reversible y privado | borrador de respuesta en Gmail, rama y PR en borrador, informe HTML, recordatorio |
| `external` | Irreversible o visible para terceros | enviar un email, reservar, pagar, desplegar, mergear, publicar |

El nivel decide qué clases corren solas y cuáles preguntan antes (con botones en Telegram):

| Nivel | `read` | `local` | `external` |
|---|---|---|---|
| `cautious` — solo mira | solo | pregunta | pregunta |
| `balanced` — mira y prepara (recomendado) | solo | solo | pregunta |
| `autonomous` — mira, prepara y actúa | solo | solo | solo únicamente las operaciones listadas en `trust.auto_external`; pagar, reservar, desplegar y mergear nunca |

**Ejemplo 1.** Un cliente escribe por email reportando un bug de login en `org/app` (configurado en `workspaces`).

- `cautious`: te llega un aviso con lo que dice el email y el botón «¿Investigo?». Solo tras tu sí reproduce el bug.
- `balanced`: reproduce el bug en una sesión acotada, crea una rama y un PR **en borrador**, y te avisa con el resumen, el enlace al PR y un borrador de respuesta. Para enviar la respuesta te pregunta.
- `autonomous` con `auto_external: [gmail.reply]`: además envía la respuesta al cliente sin preguntar. Mergear el PR o desplegar sigue pidiendo confirmación siempre.

### Qué pasa cuando autorizas una acción externa

El aviso trae botones con las opciones del paso (por ejemplo «Sí, crea el CNAME con el proxy desactivado»). Al pulsar uno:

1. La propuesta queda autorizada y el mensaje se edita con `Autorizado: …` seguido de `En marcha; te aviso con el resultado` o de `Todavía no se ha ejecutado: <motivo>` (otro job en curso, cupo diario agotado, jobs desactivados).
2. Si la operación es nativa (`gmail.reply`, `calendar.respond`, `github.comment`) y sus parámetros se resuelven de la tarea, el plugin la ejecuta él mismo: registra un consentimiento de un solo uso ligado a la operación y a los parámetros exactos, lo consume al actuar, actúa una vez y verifica releyendo. En cualquier otro caso se lanza un job `hermes chat` que lleva la autorización exacta (paso, opción, hora) y la regla de hacer solo esa acción, verificarla releyendo el resultado y listar lo que cambió.
3. Al terminar, el resultado vuelve al pipeline y recibes un aviso con lo hecho y lo que quede pendiente. Ese aviso no cuenta para el cupo diario de avisos (`notices_per_day`, 8 por defecto, en el formulario de Desktop): lo que responde a una autorización tuya llega siempre.

Si no pudo arrancar, la propuesta queda en estado `waiting` (en `/radar status` aparece como «en espera», en `/radar list` con ⏸) y se reintenta en cada tick hasta que el motivo desaparece (por ejemplo, cuando se renueva el cupo diario). Pulsar «No» sobre el aviso la cancela.

### Quién actúa: escalado

Antes de avisarte, el planificador decide si el asistente puede encargarse solo (`handle`: lee la notificación, consulta el mensaje, comprueba los pagos; no te llega nada hasta que hay resultado), si solo hay que contarte algo (`inform`: un mensaje con la noticia y un botón «Vale», sin preguntas; si la noticia cierra una factura o una devolución que Radar seguía, ofrece marcarla con un botón) o si de verdad te toca decidir (`ask`). Bajo `cautious` lo de `handle` también pregunta, porque ningún paso corre solo.

### Borradores de respuesta

Cuando la tarea viene de un hilo de correo, el aviso lleva un bloque «Correo» con el remitente del último mensaje, la fecha en tu zona horaria, el asunto, el número de mensajes del hilo y un extracto del último (sin el texto citado de mensajes anteriores). Si vigilas varias cuentas, indica cuál.

Si Radar prepara una respuesta (clase `local`), la deja guardada como borrador en tu Gmail y te la enseña completa en el aviso, con «Enviar borrador» y «Revisarlo primero». Solo el primer botón envía, y envía exactamente ese texto. «Revisarlo primero» te manda el borrador en un mensaje aparte sin cambiar nada; puedes responder «sí» a ese mensaje para enviarlo o «no» para descartarlo. Si el borrador es muy largo, llega además como archivo adjunto.

### Freno a los bucles

Por tarea corren como máximo `auto_jobs_per_task` jobs automáticos al día (3 por defecto) con `auto_job_cooldown_minutes` entre uno y otro (15). Tu autorización explícita no está sujeta a ese límite. El cupo diario total es `jobs_per_day` (100 por defecto). Los tres están en el formulario del plugin en Desktop (Capabilities → Plugins → radar → Settings) como «Jobs per day», «Automatic jobs per task» y «Minutes between automatic jobs». Un paso que descartas con «No» no se vuelve a proponer, ni reformulado, durante 24 h; el planificador recibe la lista de lo que rechazaste.

**Ejemplo 2.** Llega la confirmación de un vuelo del 3 al 5 y no hay hotel para esas noches.

- `cautious`: aviso «falta alojamiento» y botón «¿busco opciones?».
- `balanced`: busca opciones solo y te manda tres candidatos con precio. Reservar te lo pregunta.
- `autonomous`: igual que `balanced`: reservar y pagar nunca se automatizan.

Afina por tipo de tarea con `trust.overrides`, por ejemplo `{work_item: {local: auto}}` para que las incidencias preparen PRs aunque el nivel sea `cautious`, o `{trip: {local: ask}}` para que los viajes pregunten antes de crear recordatorios. Cambia el nivel cuando quieras con `/radar trust <nivel>` en el chat, desde Hermes Desktop (Capabilities → Plugins → radar → Settings → *Trust level*) o editando `trust_level` en la configuración; ese valor plano tiene prioridad sobre `trust.level`.

Verifica el destino desde el chat del propietario: `/radar verify` (el gateway contesta él mismo a los comandos que no conoce, por eso la verificación va por el registro de comandos del host y no por un handler de Telegram).

## 3. Gmail: polling o push

Por defecto cada cuenta se sincroniza por **polling**: una búsqueda `search_emails` cada `interval_seconds` (300 s), sin modelo, con cursor por fecha. Es suficiente para la mayoría y no necesita nada fuera de Hermes.

El **push** (Pub/Sub de Gmail) es opcional y se activa por cuenta: entrega en segundos y permite subir `interval_seconds` (el polling sigue como red de seguridad si el watch caduca o se pierde un aviso). Requiere un endpoint HTTPS público (túnel) y un proyecto de GCP:

1. En GCP: topic Pub/Sub (p. ej. `projects/<id>/topics/gmail`) con permiso `pubsub.publisher` para `gmail-api-push@system.gserviceaccount.com`, y una **suscripción push** con autenticación OIDC (cuenta de servicio) hacia `https://<tu-tunel>/?token=<RADAR_GMAIL_PUSH_TOKEN>`.
2. En `setup`, activa push para cada cuenta: topic y directorio de credenciales del MCP (`credentials.json` + `gcp-oauth.keys.json`).
3. `setup` instala la ruta `gmail-push` del webhook del host (`deliver: log`, sin prompt: **no hay turno de agente**) y escribe `RADAR_GMAIL_PUSH_TOKEN` / `RADAR_GMAIL_PUSH_HERMES_SECRET` en `~/.hermes/.env`.
4. Activa el receptor dentro del gateway (`ingress.gmail_receiver.enabled: true`, puerto 8666 por defecto) y apunta el túnel a ese puerto. Alternativa: `hermes radar gmail receiver` en primer plano o como servicio.
5. Renueva el watch: `hermes radar gmail watch` (el scheduler lo renueva cada 24 h; expira a los 7 días).

Para volver a polling basta con `push.enabled: false` en la cuenta; el receptor se apaga con `ingress.gmail_receiver.enabled: false`.

### GitHub

Para los work_items de un repo, `workspaces: {org/repo: /ruta/al/checkout}` indica dónde trabajar: cada job corre en un worktree aislado de ese checkout (`jobs.worktree: true`), sube su rama y no abre el PR; Radar lo abre en borrador en un paso aparte, con marca de origen, y nunca mergea.

Una fuente `github` puede observar una ruta del webhook del host (`route: github-pr`, `consume: false` si otra cosa ya la usa, como un gatekeeper de PRs), consumir una ruta propia (`hermes radar github route github-radar` y luego `consume: true`; en GitHub, payload URL `<base del webhook>/webhooks/github-radar`, secreto el de la ruta), y sondear repos con `gh` (`repos: [org/repo]`, API de eventos con ETag, primera lectura como línea base). `gh` debe estar autenticado para el usuario que ejecuta el gateway (`gh auth status`). Los eventos se filtran antes del modelo (ver README) y las señales llevan repo, tipo, número, actor, rama y enlace.

### Calendario

`discover` reconoce el MCP de Google Calendar y `setup` propone una fuente `calendar` (`calendar_id: primary`, cada 10 minutos). Cada lectura compara los eventos de la ventana (`lookback_hours`, `lookahead_days`) con la huella guardada: un evento nuevo, movido, renombrado, cancelado o al que has respondido genera una señal; el resto no. La primera lectura solo registra la línea base (`initial_import: true` si quieres que los eventos ya existentes también entren). Los eventos declinados no se siguen. Para varias cuentas en el mismo servidor, `account` indica el apodo del MCP.

### Memoria y sesiones anteriores

Antes de planificar una tarea, Radar consulta dos fuentes de contexto del host, ambas opcionales (`context.memory`, `context.sessions`):

- La **memoria** del proveedor activo (`memory.provider` en `config.yaml`): se usa su propia herramienta de búsqueda, sea hindsight, mnemosyne u otro que implemente `MemoryProvider` (`context.memory_tool` fija el nombre si la detección automática no acierta). El proveedor se carga en modo solo lectura; Radar no escribe en la memoria.
- Las **sesiones anteriores** con el agente, vía `session_search` del host (`context.session_results`).

Lo recordado va al modelo como datos, acotado por `context.max_chars` y `context.timeout_seconds`; si algo falla o tarda, la tarea se planifica sin ello.

### Preferencias

Responde a cualquier aviso con lo que quieres cambiar y Radar lo guarda como preferencia con alcance: «no me avises más de este remitente» (silencio para esa dirección), «los de CI mándamelos en el resumen» (digest), «avísame siempre de esto» (interrumpir), «silencia toda esta cuenta». El modelo interpreta la frase con el contexto del aviso; sin modelo se guarda tal cual con el alcance del remitente o del tipo de tarea. Las alertas de seguridad, cortes de servicio, plazos y pérdidas económicas siguen pasando salvo que digas lo contrario.

- `/radar prefs` lista las activas; `/radar forget <id>` desactiva una (quedan versionadas, nunca se borran).
- En el chat, el agente puede guardarlas con `radar_preference` (efecto y alcance explícitos).
- Se aplican desde la siguiente señal: el triaje descarta lo silenciado antes de llamar al modelo y la atención decide aviso inmediato o resumen.

## 4. Patrones y panel

Una hora después de arrancar, y luego cada 24 h, Radar busca patrones en las señales (correo, GitHub, turnos de chat, resultados de jobs). Cuando encuentra uno que merece automatizarse te llega una propuesta «Automatizar» en el resumen (nunca se ejecuta sola); aceptarla crea un cron del host que entrega en tu chat y cuyas salidas vuelven al radar; descartarla lo silencia. `/radar patterns` muestra lo detectado.

El panel de Hermes Desktop se carga solo con el paquete enlazado en `~/.hermes/plugins/radar` (Capabilities → Plugins → Rescan si no aparece; el plugin debe estar en `plugins.enabled` para que su backend se monte). Muestra las tareas por estado, el detalle con el hilo, las propuestas con los mismos botones que Telegram, borradores, informe, patrones y preferencias.

## 5. Comprobar

```bash
hermes radar status
hermes radar sync gmail-work         # fuerza una lectura
hermes radar signals                 # triaje de las últimas señales
hermes radar run-once                # un ciclo completo: sync, pipeline, jobs, avisos
hermes radar tasks
hermes radar explain <task_id> --html
```

En Telegram: cada tarea nueva con huecos llega como un aviso con propuestas numeradas y botones; "Más tarde" pospone 24 h y "No avisar de esto" silencia la tarea. También puedes responder al aviso con texto ("sí", "la 2", "mañana", "ya está hecho") o pedírselo al agente en el chat (`radar_resolve`).

## 6. Migrar desde mail-intelligence

```bash
hermes radar import mail-intelligence      # solo preferencias activas
systemctl --user disable --now gmail-push.service   # el receptor pasa al gateway
```

Quita `mail-intelligence` de `plugins.enabled`. Su repo queda como archivo; sus casos y decisiones no se importan.
