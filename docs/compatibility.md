# Proactive Hermes — compatibilidad 0.2.0

## Host de referencia

- Checkout: `/home/naroh/.hermes/hermes-agent`
- Commit inspeccionado: `f9c3a8a1863b17d479335f804e04749aff7f5d84`
- APIs públicas usadas: `register(ctx)`, `ctx.register_tool`, `ctx.register_hook`, `ctx.register_platform_handler`, `ctx.register_cli_command`, `ctx.on_unload`, `ctx.spawn_task`, `ctx.get_config`, `ctx.profile_name` y `hermes_constants.get_hermes_home`.
- No se modifican ni importan APIs privadas del host.

## Evidencia ejecutada

- `hermes plugins doctor . --ci` con `HERMES_HOME` temporal: **pasa sin avisos**.
- Descubrimiento: `proactive 0.2.0 (standalone)`.
- Registro: cuatro herramientas y cuatro hooks.
- CLI real: `hermes proactive validate` confirma la configuración activa del perfil `default`.
- `uv run --with pytest python -m pytest -q`: **37 passed**.
- `python3 -m compileall -q proactive tests`: **pasa**.
- Parseo de los dos JSON Schema y `git diff --check`: **pasan**.
- Integración HTTPS real aislada contra `https://example.com/`: scheduler reclamó un seguimiento, creó plan y run, ejecutó una lectura y persistió `http_status=200` con schema 6.
- El checkout de Hermes no recibió cambios de esta implementación. Conserva el directorio no relacionado `plugins/context_engine/lcm/` que ya existía sin seguimiento.

## Contratos simulados

- Scheduler único por contexto, cancelación en unload y cierre del store.
- Actualización de una base bootstrap v2 a schema 6.
- Scope aislado por perfil y propietario.
- Autorización y revocación de grants antes de red.
- Presupuesto reservado y consumido de forma transaccional.
- Run con lease, fencing token e idempotency key.
- Outbox con deduplicación, lease, fencing, retry y receipt.
- Transporte neutral y entrega simulada por Telegram.
- Consentimiento HMAC ligado a propuesta, plan, actor, destino, caducidad y nonce.
- Consumo único y bloqueo de retry ciego cuando una mutación simulada queda en estado `unknown`.

## Compatibilidad de transportes

| Plataforma | Registro neutral | Verificación de actor/destino | Envío implementado | Sandbox real |
|---|---:|---:|---:|---:|
| Telegram | Sí | Sí, mediante `/proactive_verify` | Sí | Verificado: entrega privada con `platform_message_id=10892` |
| Otras | Sí, mediante `TransportRegistry` | Requiere adaptador | No | No ejecutado |

No se declara compatibilidad E2E con una plataforma sin una cuenta de sandbox autorizada.

## Pendiente externo

El perfil `default` está activo y el destino privado de Telegram está verificado. Una fuente temporal que devolvía HTTP 503 completó plan, run y outbox; Telegram confirmó `platform_message_id=10892`. La fuente se retiró después de la prueba. Queda pendiente:

1. repetir el contrato en una segunda plataforma autorizada;
2. implementar y reconciliar una operación mutante contra un proveedor de prueba.

La versión 0.2.0 no publica operaciones externas de mutación. Sí incluye y prueba el executor central y el consentimiento de uso único con una operación simulada, incluida la salida incierta sin retry automático.
