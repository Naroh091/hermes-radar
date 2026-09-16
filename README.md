# Proactive Hermes

Plugin externo para detectar seguimientos y ejecutar consultas informativas autorizadas. Esta primera fase implementa la base instalable, configuración cerrada, SQLite propio por perfil/propietario, catálogo de `http_status.check` y herramientas de diagnóstico. El plugin queda **desactivado** por defecto.

## Instalación aislada

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Para probar el descubrimiento sin activar fuentes reales, copia el paquete a un perfil de prueba y habilítalo solo en ese perfil. No edites `run_agent.py`, `gateway/*` ni `hermes_cli/*`.

Configuración mínima:

```yaml
plugins:
  enabled: [proactive]
  entries:
    proactive:
      settings:
        enabled: false
        mode: shadow
        owner: {id: test-owner, timezone: Europe/Madrid, language: es}
        sources: []
        allowed_operations: []
```

`proactive_propose` no aprueba ni ejecuta mutaciones. Las consultas informativas futuras exigirán una fuente, recurso, grant y allowlist explícitos. Las credenciales nunca se guardan en esta configuración.

## Estado de esta entrega

- P0 parcial: `hermes plugins doctor . --ci` pasa en `HERMES_HOME` temporal; registra 4 herramientas y 1 hook. El ciclo de vida con event loop y sandbox de plataforma sigue pendiente.
- P1 inicial: migraciones 001–002, scope compuesto y catálogo cerrado.
- Scheduler y detectores aún no deben considerarse completos. El código no activa red, LLM, hooks de contenido ni destinos con `enabled: false`.

## Verificación

```bash
python -m pytest
hermes plugins doctor . --ci
```

El segundo comando debe ejecutarse con una instalación de Hermes compatible y un `HERMES_HOME` temporal. La prueba P0 debe guardar su resultado en `docs/compatibility.md`.
