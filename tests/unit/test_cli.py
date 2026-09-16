from __future__ import annotations

import argparse
import json

from proactive.cli import command


class DisabledContext:
    profile_name = "default"

    @staticmethod
    def get_config(key, default=None):
        return False if key == "enabled" else default


def test_disabled_cli_status_does_not_require_owner_or_open_store(capsys):
    result = command(DisabledContext(), argparse.Namespace(proactive_action="status"))
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {"enabled": False, "configured": False, "mode": "shadow", "store": None}
