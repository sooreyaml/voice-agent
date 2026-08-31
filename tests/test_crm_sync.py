"""Post-call CRM sync: enqueue on completion, drain to contact + note + task,
idempotent retries, and dead-lettering."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domains.integrations import crm_sync
from app.domains.integrations import service as integ_service
from app.domains.integrations.base import CrmContact, CrmProviderError
from app.domains.integrations.crypto import build_cipher
from app.settings import load_settings
from app.store import Store


def _cipher():
    return build_cipher(
        replace(load_settings(), integration_encryption_key="", environment="development")
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "calls.sqlite3")


class FakeCrm:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contacts: list[CrmContact] = []
        self.notes: list[tuple[str, str]] = []
        self.tasks: list[tuple[str, str, str | None]] = []

    def verify(self) -> dict:
        return {"external_account_id": "portal-1"}

    def find_contact(self, *, phone=None, email=None):
        return None

    def upsert_contact(self, *, name=None, phone=None, email=None) -> CrmContact:
        if self.fail:
            raise CrmProviderError("hubspot_http_503", "down", retryable=True)
        contact = CrmContact(id=f"contact-{len(self.contacts) + 1}", name=name, phone=phone)
        self.contacts.append(contact)
        return contact

    def add_note(self, *, contact_id: str, body: str) -> str:
        self.notes.append((contact_id, body))
        return f"note-{len(self.notes)}"

    def create_task(self, *, contact_id, title, body=None, due_at=None) -> str:
        self.tasks.append((contact_id, title, body))
        return f"task-{len(self.tasks)}"


def _connect_hubspot(store: Store, org_id: str) -> None:
    store.upsert_integration_connection(
        org_id,
        "hubspot",
        status="active",
        display_name=None,
        encrypted_credentials=_cipher().seal({"access_token": "pat-secret"}),
        external_account_id="portal-1",
        scopes=None,
        settings="{}",
        last_error=None,
        last_verified_at=datetime.now(UTC).replace(microsecond=0),
    )


def _seed_call(store: Store, org_id: str, call_id: str = "c1") -> None:
    store.start_call(org_id, call_id, "Acme", "+16175550188", "+16175550142")
    store.add_lead(
        org_id,
        call_id,
        {
            "intent": "pricing_question",
            "details": "wants a quote for 3 units",
            "caller_name": "Dana Scully",
        },
    )
    store.finish_call(
        org_id, call_id, "completed", json.dumps({"summary": "Asked about pricing."}), 0.0
    )


def test_enqueue_and_drain_creates_contact_note_and_task(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    org_id = store.ensure_organization("acme", "Acme")
    _connect_hubspot(store, org_id)
    _seed_call(store, org_id)

    job_id = crm_sync.enqueue_call_sync(store, org_id, "c1")
    assert job_id is not None
    # enqueuing the same call again is a no-op (unique constraint)
    assert crm_sync.enqueue_call_sync(store, org_id, "c1") is None

    fake = FakeCrm()
    monkeypatch.setattr(integ_service, "load_crm_provider", lambda *a, **k: fake)

    processed = crm_sync.process_due_crm_jobs(store, _cipher())
    assert processed == 1

    assert [c.name for c in fake.contacts] == ["Dana Scully"]
    assert len(fake.notes) == 1
    assert "Asked about pricing." in fake.notes[0][1]
    assert "Captured need: pricing_question" in fake.notes[0][1]
    assert fake.tasks == [("contact-1", "Follow up: pricing_question", "wants a quote for 3 units")]

    job = store.crm_sync_job(org_id, job_id)
    assert job["status"] == "succeeded"
    result = json.loads(job["result"])
    assert result["contact_id"] == "contact-1"
    assert result["note_id"] == "note-1" and result["tasks_done"] is True

    # a second drain finds nothing due
    assert crm_sync.process_due_crm_jobs(store, _cipher()) == 0


def test_retry_after_partial_result_does_not_duplicate(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    org_id = store.ensure_organization("acme", "Acme")
    _connect_hubspot(store, org_id)
    _seed_call(store, org_id)
    job_id = crm_sync.enqueue_call_sync(store, org_id, "c1")

    fake = FakeCrm()
    monkeypatch.setattr(integ_service, "load_crm_provider", lambda *a, **k: fake)
    crm_sync.process_due_crm_jobs(store, _cipher())

    # Pretend the job failed *after* completing, keeping its result, and is due.
    store.finish_crm_sync_job(
        job_id,
        status="failed",
        attempts=1,
        next_attempt_at=datetime(2000, 1, 1, tzinfo=UTC),
        last_error="transient",
        result=store.crm_sync_job(org_id, job_id)["result"],
    )
    crm_sync.process_due_crm_jobs(store, _cipher())

    assert len(fake.contacts) == 1 and len(fake.notes) == 1 and len(fake.tasks) == 1
    assert store.crm_sync_job(org_id, job_id)["status"] == "succeeded"


def test_provider_failure_retries_then_dead_letters(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    org_id = store.ensure_organization("acme", "Acme")
    _connect_hubspot(store, org_id)
    _seed_call(store, org_id)
    job_id = crm_sync.enqueue_call_sync(store, org_id, "c1", max_attempts=2)

    monkeypatch.setattr(
        integ_service, "load_crm_provider", lambda *a, **k: FakeCrm(fail=True)
    )

    crm_sync.process_due_crm_jobs(store, _cipher())
    job = store.crm_sync_job(org_id, job_id)
    assert job["status"] == "failed" and job["attempts"] == 1
    assert job["next_attempt_at"] is not None

    store.execute(
        "UPDATE crm_sync_jobs SET next_attempt_at = ?",
        (datetime(2000, 1, 1, tzinfo=UTC),),
    )
    crm_sync.process_due_crm_jobs(store, _cipher())
    dead = store.crm_sync_job(org_id, job_id)
    assert dead["status"] == "dead" and dead["attempts"] == 2


def test_enqueue_is_noop_without_a_crm_connection(store: Store):
    org_id = store.ensure_organization("acme", "Acme")
    _seed_call(store, org_id)
    assert crm_sync.enqueue_call_sync(store, org_id, "c1") is None
    assert store.query("SELECT COUNT(*) AS n FROM crm_sync_jobs")[0]["n"] == 0
