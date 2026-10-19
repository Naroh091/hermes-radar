"""Profile secret scope for background work on a multiplexed gateway.

Under ``gateway.multiplex_profiles`` every credential read must run inside the profile's secret scope
(``agent.secret_scope.set_secret_scope``); the per-turn and per-adapter paths install it, but a task the
plugin spawns itself does not inherit one. The scheduler enters the scope for its own profile home
around each tick. Outside the gateway, or when a scope is already installed, this is a no-op.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _load_scope(home: Path, build: Any) -> dict[str, str]:
    env_path = home / ".env"
    try:
        mtime = env_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _cache.get(str(home))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    secrets = dict(build(home))
    _cache[str(home)] = (mtime, secrets)
    return secrets


@contextmanager
def profile_secret_scope(hermes_home: str | Path | None) -> Iterator[None]:
    try:
        from agent.secret_scope import (
            build_profile_secret_scope, current_secret_scope, is_multiplex_active, reset_secret_scope,
            set_secret_scope,
        )
    except ImportError:  # unit tests or a host without multiplexing
        yield
        return
    if hermes_home is None or current_secret_scope() is not None or not is_multiplex_active():
        yield
        return
    home = Path(hermes_home)
    try:
        secrets = _load_scope(home, build_profile_secret_scope)
    except Exception as exc:
        log.warning("radar could not build the profile secret scope for %s: %s", home, exc)
        yield
        return
    token = set_secret_scope(secrets, profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)
