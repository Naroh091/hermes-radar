# Proactive Hermes

Plugin externo de seguimiento seguro para Hermes Agent. La versión 0.2.0 incorpora observación local opt-in, detectores conservadores, monitores HTTPS autorizados, scheduler persistente, presupuestos, fencing, auditoría, outbox y transporte Telegram verificado. El plugin queda **desactivado** por defecto.

## Instalación aislada

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

No edites `run_agent.py`, `gateway/*` ni `hermes_cli/*`. El plugin usa solo las APIs públicas declaradas en `docs/compatibility.md`.

## Configuración segura de prueba

```yaml
plugins:
  enabled:
    - proactive
  entries:
    proactive:
      allow_tool_override: false
      settings:
        enabled: true
        mode: shadow
        owner:
          id: test-owner
          timezone: Europe/Madrid
          language: es
        observation:
          local_sessions: false
          historical_import: false
        scheduler:
          tick_seconds: 60
          max_concurrent_checks: 2
        sources:
          - id: example-status
            kind: http_status
            url: https://example.com/
            interval_seconds: 300
            retention_days: 7
            enabled: true
            operations: [http_status.check]
        allowed_operations:
          - http_status.check
```

En `shadow`, el scheduler ejecuta lecturas autorizadas y persiste decisiones, planes, runs y auditoría. No crea notificaciones ni mutaciones.

Valida y prueba un ciclo explícito con:

```bash
hermes proactive validate
hermes proactive run-once
hermes proactive status
```

## Notificaciones por Telegram

Las notificaciones exigen `mode: active`, un actor concreto y un chat concreto:

```yaml
mode: active
destination:
  platform: telegram
  gateway_connection_ref: telegram:default
  resolution: explicit
  address:
    chat_id: "123456789"
  owner_platform_id: "123456789"
  allow_groups: false
  verified: false
notifications:
  enabled: true
  quiet_start: "22:00"
  quiet_end: "08:00"
  daily_limit: 3
  batch_minutes: 30
  reminder_cooldown_hours: 24
```

`verified` nunca puede fijarse a `true` en YAML. El propietario debe enviar `/proactive_verify` desde el actor y chat configurados. El plugin guarda entonces el destino verificado. Los grupos se rechazan salvo autorización explícita.

La entrega usa una outbox persistente, clave de notificación única, lease, fencing token, reintentos con backoff y `platform_message_id` real.

## Seguridad

- Scope obligatorio por `(profile_key, owner_id)` en todas las operaciones.
- Configuración y objetos principales cerrados a campos desconocidos.
- Fuentes y grants se materializan de forma idempotente desde configuración.
- `http_status.check` exige HTTPS, puerto 443, host autorizado, DNS público, límite de respuesta y redirects desactivados.
- Los hooks locales no guardan prompts, respuestas, argumentos ni resultados completos. Solo persisten metadatos acotados y candidatos estructurados.
- El modelo no autoriza acciones. La policy valida catálogo, operación habilitada, source, grant, recurso y revisión de permisos.
- Los tokens de consentimiento ligan propuesta, hash del plan, actor, destino, caducidad y nonce mediante HMAC. Un consentimiento aprobado se consume una sola vez.
- No hay credenciales en la configuración del plugin.
- Las mutaciones externas siguen desactivadas y no existe ninguna operación de mutación en el catálogo 0.2.0.

## Estado

Implementado y probado:

- seis migraciones y 16 tablas de dominio;
- actualización desde el snapshot v2 inicial;
- repositorios aislados por scope;
- observaciones deduplicadas y retención;
- detectores locales de compromiso explícito, plazo y input ausente;
- sincronización de monitores HTTPS y grants;
- scheduler con recuperación de claims, presupuesto transaccional, planes, runs, leases y fencing;
- consulta HTTPS real mediante `http_status.check`;
- destino Telegram verificado, outbox y entrega con receipt;
- consentimiento firmado, contextual y de uso único;
- CLI de validación, estado y ejecución manual;
- cuatro herramientas y cuatro hooks públicos.

No certificado sin autorización externa:

- entrega real en una cuenta Telegram de prueba;
- adaptador y sandbox de una segunda plataforma;
- una mutación externa real y su reconciliación;
- importación histórica, porque el host no expone aún un lector público de sesiones para este plugin.

El plugin no activa cuentas, destinos ni mutaciones durante la instalación.

## Verificación

```bash
uv run --with pytest python -m pytest -q
python3 -m compileall -q proactive tests
hermes plugins doctor . --ci
uv build
```
