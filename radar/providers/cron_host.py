"""The host's cron as a provider: create scheduled jobs and read their outputs (gateway process only)."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class HostCronProvider:
    provider_id = "cron_host"

    def __init__(self, *, allow_write: bool = False, module: Any = None):
        self.read_only = not allow_write
        self._module = module

    def _cron(self) -> Any:
        if self._module is None:
            from cron import jobs as cron_jobs  # type: ignore[import-not-found]  # host package

            self._module = cron_jobs
        return self._module

    def create(self, *, prompt: str, schedule: str, name: str, origin: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("cron.create is not permitted on this provider")
        record = self._cron().create_job(prompt=prompt, schedule=schedule, name=name, origin=origin, deliver="origin" if origin else None)
        return {"id": str((record or {}).get("id") or ""), "name": str((record or {}).get("name") or name), "schedule": schedule}

    def get(self, job_id: str) -> dict[str, Any] | None:
        record = self._cron().get_job(job_id)
        return dict(record) if isinstance(record, dict) else None

    def output_dir(self) -> Path:
        return Path(self._cron().get_cron_output_dir())
