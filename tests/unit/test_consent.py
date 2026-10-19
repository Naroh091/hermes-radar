from __future__ import annotations

from pathlib import Path
import asyncio
from datetime import datetime, timezone

import pytest

from radar.consent import ConsentSigner
from radar.executor import MutationExecutor
from radar.store import Scope, Store

NOW = "2026-09-16T10:00:00Z"
EXPIRY = "2026-09-16T11:00:00Z"


def prepared_store(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))
    store.add_goal("goal-1", "Mutation test", NOW)
    store.add_followup(
        followup_id="followup-1", goal_id="goal-1", reason="next_step", status="awaiting_consent",
        subject_key="mutation:fixture", evidence_revision=1, expires_at=EXPIRY,
        timezone="UTC", confidence_band="high", assumptions={}, stop_rule={}, now=NOW,
    )
    plan = store.add_plan(
        plan_id="plan-1", followup_id="followup-1", operation_id="fixture.mutate",
        operation_version="1", parameters={"value": "x"}, effect_class="external",
        evidence_revision=1, permission_revision=1, now=NOW,
    )
    store.verify_destination(
        destination_id="destination-1", platform="telegram", gateway_connection_ref="telegram:default",
        address={"chat_id": "42"}, owner_platform_id="7", kind="private",
        capabilities={"text": True, "buttons": True}, now=NOW,
    )
    store.create_proposal(
        proposal_id="proposal-1", followup_id="followup-1", plan_id="plan-1",
        destination_id="destination-1", expires_at=EXPIRY, priority="normal",
        reason_text="Create fixture", suppression_key="fixture:1", now=NOW,
    )
    return store, plan


def test_consent_token_binds_full_context_and_rejects_tampering(tmp_path: Path):
    signer = ConsentSigner.from_file(tmp_path / "consent.key")
    token = signer.issue(
        proposal_id="proposal-1", plan_hash="abc", actor_ref="telegram:7",
        destination_id="destination-1", expires_at=EXPIRY,
    )

    claims = signer.verify(token)

    assert claims.proposal_id == "proposal-1"
    assert claims.plan_hash == "abc"
    assert claims.actor_ref == "telegram:7"
    with pytest.raises(ValueError, match="signature"):
        signer.verify(token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert (tmp_path / "consent.key").stat().st_mode & 0o777 == 0o600


def test_approved_consent_is_exact_and_single_use(tmp_path: Path):
    store, plan = prepared_store(tmp_path)
    signer = ConsentSigner.from_file(tmp_path / "consent.key")
    token = signer.issue(
        proposal_id="proposal-1", plan_hash=plan["plan_hash"], actor_ref="telegram:7",
        destination_id="destination-1", expires_at=EXPIRY,
    )
    claims = signer.verify(token)
    store.record_consent(
        consent_id="consent-1", proposal_id=claims.proposal_id, plan_hash=claims.plan_hash,
        decision="approved", actor_ref=claims.actor_ref, verified_transport="telegram",
        destination_id=claims.destination_id, expires_at=claims.expires_at,
        token_hash=signer.token_hash(token), now=NOW,
    )

    store.consume_consent(
        "consent-1", plan_hash=plan["plan_hash"], actor_ref="telegram:7",
        destination_id="destination-1", verified_transport="telegram", now=NOW,
    )

    with pytest.raises(RuntimeError, match="already consumed"):
        store.consume_consent(
            "consent-1", plan_hash=plan["plan_hash"], actor_ref="telegram:7",
            destination_id="destination-1", verified_transport="telegram", now=NOW,
        )
    assert store.db.execute("SELECT status FROM proposals").fetchone()[0] == "accepted"
    store.close()


def test_mutation_executor_never_retries_uncertain_side_effect(tmp_path: Path):
    store, plan = prepared_store(tmp_path)
    signer = ConsentSigner.from_file(tmp_path / "consent.key")
    token = signer.issue(
        proposal_id="proposal-1", plan_hash=plan["plan_hash"], actor_ref="telegram:7",
        destination_id="destination-1", expires_at=EXPIRY,
    )
    claims = signer.verify(token)
    store.record_consent(
        consent_id="consent-1", proposal_id=claims.proposal_id, plan_hash=claims.plan_hash,
        decision="approved", actor_ref=claims.actor_ref, verified_transport="telegram",
        destination_id=claims.destination_id, expires_at=claims.expires_at,
        token_hash=signer.token_hash(token), now=NOW,
    )
    calls: list[str] = []

    class FixtureMutation:
        id = "fixture.mutate"
        effect_class = "external"

        @staticmethod
        def validate_parameters(parameters):
            assert parameters == {"value": "x"}

        @staticmethod
        def execute(scope, parameters, context):
            calls.append(context["idempotency_key"])
            raise TimeoutError("provider outcome is uncertain")

    executor = MutationExecutor(
        store, {"fixture.mutate": FixtureMutation()}, worker_id="worker-1",
        clock=lambda: datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc),
    )
    result = asyncio.run(
        executor.execute(
            plan_id="plan-1", consent_id="consent-1", actor_ref="telegram:7",
            destination_id="destination-1", verified_transport="telegram",
        )
    )

    assert result["status"] == "unknown"
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="already consumed"):
        asyncio.run(
            executor.execute(
                plan_id="plan-1", consent_id="consent-1", actor_ref="telegram:7",
                destination_id="destination-1", verified_transport="telegram",
            )
        )
    assert len(calls) == 1
    assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "unknown"
    store.close()
