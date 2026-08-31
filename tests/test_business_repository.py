from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.business import BusinessProfile
from app.domains.businesses.repository import (
    BusinessRepository,
    PhoneNumberAlreadyAssigned,
)
from app.store import Store

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"


def _profile(slug: str = "harborview-dental") -> BusinessProfile:
    loaded = BusinessProfile.load(BUSINESSES / "harborview-dental.yaml")
    loaded.slug = slug
    return loaded


def test_published_profile_is_routed_from_database_after_reopen(tmp_path: Path):
    database_path = tmp_path / "profiles.sqlite3"
    store = Store(database_path)
    published = BusinessRepository(store).publish(_profile())
    store.close()

    reopened = Store(database_path)
    routed = BusinessRepository(reopened).find_by_phone_number("+44 7723 478472")
    assert routed is not None
    assert routed.organization_id == published.organization_id
    assert routed.profile_id == published.profile_id
    assert routed.version_id == published.version_id
    assert routed.phone_numbers == ["+447723478472"]


def test_unchanged_import_reuses_version_and_changed_config_publishes_next_version(
    tmp_path: Path,
):
    store = Store(tmp_path / "profiles.sqlite3")
    repository = BusinessRepository(store)
    profile = _profile()
    first = repository.publish(profile)
    unchanged = repository.publish(profile)
    assert unchanged.version_id == first.version_id
    assert unchanged.version_number == 1

    changed = _profile()
    changed.raw["agent"]["greeting"] = "A newly published greeting."
    second = repository.publish(changed)
    assert second.version_id != first.version_id
    assert second.version_number == 2
    statuses = store.query(
        "SELECT id, status FROM agent_versions ORDER BY version_number"
    )
    assert statuses == [
        {"id": first.version_id, "status": "archived"},
        {"id": second.version_id, "status": "published"},
    ]


def test_call_keeps_exact_profile_version_after_new_publish(tmp_path: Path):
    store = Store(tmp_path / "profiles.sqlite3")
    repository = BusinessRepository(store)
    first = repository.publish(_profile())
    store.start_call(
        first.organization_id,
        "call-on-version-one",
        first.name,
        "+15550000001",
        first.phone_numbers[0],
        first.version_id,
    )

    changed = _profile()
    changed.raw["agent"]["greeting"] = "Version two greeting."
    second = repository.publish(changed)
    assert second.version_id != first.version_id
    detail = store.call_detail(first.organization_id, "call-on-version-one")
    assert detail is not None
    assert detail["agent_version_id"] == first.version_id


def test_phone_number_cannot_be_claimed_by_another_organization(tmp_path: Path):
    store = Store(tmp_path / "profiles.sqlite3")
    repository = BusinessRepository(store)
    repository.publish(_profile())

    other = _profile("other-business")
    other.raw = copy.deepcopy(other.raw)
    other.raw["business"]["name"] = "Other Business"
    with pytest.raises(PhoneNumberAlreadyAssigned, match="already assigned"):
        repository.publish(other)


def test_removed_phone_number_is_deactivated_not_deleted(tmp_path: Path):
    store = Store(tmp_path / "profiles.sqlite3")
    repository = BusinessRepository(store)
    first = repository.publish(_profile())

    changed = _profile()
    changed.raw["business"]["phone_numbers"] = ["+442071234567"]
    repository.publish(changed)

    assert repository.find_by_phone_number(first.phone_numbers[0]) is None
    assert repository.find_by_phone_number("+442071234567") is not None
    numbers = store.query(
        "SELECT e164, status FROM phone_numbers ORDER BY e164"
    )
    assert numbers == [
        {"e164": "+442071234567", "status": "active"},
        {"e164": "+447723478472", "status": "inactive"},
    ]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("business", "timezone"), "Mars/Olympus", "valid IANA timezone"),
        (("business", "phone_numbers"), [], "at least 1 item"),
        (("business", "phone_numbers"), ["07723 478472"], "leading +"),
    ],
)
def test_invalid_profile_cannot_be_published(
    tmp_path: Path,
    path: tuple[str, str],
    value: object,
    message: str,
):
    store = Store(tmp_path / "profiles.sqlite3")
    profile = _profile()
    profile.raw[path[0]][path[1]] = value
    with pytest.raises(ValidationError, match=message):
        BusinessRepository(store).publish(profile)


def test_published_profile_can_be_exported_as_yaml(tmp_path: Path):
    store = Store(tmp_path / "profiles.sqlite3")
    repository = BusinessRepository(store)
    published = repository.publish(_profile())
    exported = repository.export_yaml(published.organization_id, published.slug)
    assert exported is not None
    assert "name: Harborview Dental" in exported
    assert "+447723478472" in exported
