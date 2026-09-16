# Proactive Hermes — P0 compatibility

## Objetivo

Validar el contrato público de Hermes sin modificar su checkout ni activar cuentas reales.

## Host de referencia

- Ruta inspeccionada: `/home/naroh/.hermes/hermes-agent`
- Commit inspeccionado: `f9c3a8a1863b17d479335f804e04749aff7f5d84`
- APIs usadas por el plugin: `register(ctx)`, `ctx.register_tool`, `ctx.register_hook`, `ctx.on_unload`, `ctx.spawn_task`, `ctx.get_config`, `ctx.profile_name` y `hermes_constants.get_hermes_home`.
- No se usan métodos privados del host, parches, polling paralelo ni base de datos interna.

## Evidencia ejecutada

- `hermes plugins doctor . --ci` con `HERMES_HOME` temporal: **pasa**.
- Descubrimiento: `proactive 0.1.0 (standalone)`.
- Registro: 4 herramientas y 1 hook (`on_session_start`).
- `python -m pytest`: **4 passed**.
- Entry point instalado en un entorno virtual temporal y cargable: **pasa**.
- Checkout de Hermes: sin archivos modificados por esta implementación; conserva solo el cambio no relacionado que ya existía en `plugins/context_engine/lcm/`.

## Pendiente antes de declarar P0 completo

La tarea supervisada real, cancelación en unload, propagación de perfil y selección/entrega en dos plataformas requieren una instancia aislada de Hermes con un event loop y conexiones de sandbox autorizadas. Los mocks no certifican conexión ni entrega. Esta entrega no activa el plugin en el perfil del propietario ni conecta una plataforma.

1. Descubrimiento del `plugin.yaml` y del entry point.
2. Registro de cuatro herramientas y un hook público.
3. Configuración `enabled: false` sin red, LLM ni lectura de cuentas.
4. Al habilitarlo en perfil de prueba, tarea supervisada única y cancelación en unload.
5. Restauración de estado y ausencia de modificaciones en el checkout de Hermes.

Los mocks no certifican conexión ni entrega de una plataforma. La matriz de capacidades se añadirá cuando exista una conexión de sandbox autorizada.
