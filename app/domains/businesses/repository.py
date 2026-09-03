from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import yaml

from app.business import BusinessProfile, render_instructions
from app.store import Store

from .normalization import normalize_e164
from .schemas import PublishedBusinessConfiguration


class PhoneNumberAlreadyAssigned(ValueError):
    """An E.164 number is already owned by a different business profile."""


class DraftNotFound(ValueError):
    """The requested business profile has no unpublished draft."""


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class BusinessRepository:
    """Publishes immutable profile versions and resolves inbound numbers."""

    def __init__(self, store: Store):
        self._store = store

    @staticmethod
    def _configuration(profile: BusinessProfile) -> tuple[dict[str, Any], str]:
        validated = PublishedBusinessConfiguration.model_validate(profile.raw)
        config = copy.deepcopy(profile.raw)
        config["business"]["phone_numbers"] = validated.business.phone_numbers
        serialized = json.dumps(
            config, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return config, serialized

    @staticmethod
    def _profile_id(organization_id: str, slug: str) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"call-agent:business-profile:{organization_id}:{slug}",
            )
        )

    @staticmethod
    def _phone_id(e164: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"call-agent:phone:{e164}"))

    @staticmethod
    def _deserialize_config(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            decoded = json.loads(value)
        else:
            decoded = value
        if not isinstance(decoded, dict):
            raise TypeError("published business configuration is not an object")
        return decoded

    @classmethod
    def _from_row(cls, row: dict[str, Any]) -> BusinessProfile:
        return BusinessProfile(
            raw=cls._deserialize_config(row["config"]),
            slug=str(row["slug"]),
            organization_id=str(row["organization_id"]),
            profile_id=str(row["profile_id"]),
            version_id=str(row["version_id"]),
            version_number=int(row["version_number"]),
        )

    def _version_row_for_slug(
        self, organization_id: str, slug: str, status: str
    ) -> dict[str, Any] | None:
        rows = self._store.query(
            "SELECT business_profiles.id AS profile_id,"
            " business_profiles.organization_id, business_profiles.slug,"
            " agent_versions.id AS version_id, agent_versions.version_number,"
            " agent_versions.status, agent_versions.config,"
            " agent_versions.rendered_prompt, agent_versions.created_at,"
            " agent_versions.published_at FROM business_profiles"
            " JOIN agent_versions"
            " ON agent_versions.business_profile_id = business_profiles.id"
            " AND agent_versions.organization_id = business_profiles.organization_id"
            " WHERE business_profiles.organization_id = ?"
            " AND business_profiles.slug = ? AND agent_versions.status = ?"
            " ORDER BY agent_versions.version_number DESC LIMIT 1",
            (organization_id, slug, status),
        )
        return rows[0] if rows else None

    def _published_row_for_slug(
        self, organization_id: str, slug: str
    ) -> dict[str, Any] | None:
        return self._version_row_for_slug(organization_id, slug, "published")

    def _draft_row_for_slug(
        self, organization_id: str, slug: str
    ) -> dict[str, Any] | None:
        return self._version_row_for_slug(organization_id, slug, "draft")

    @staticmethod
    def _slug(profile: BusinessProfile) -> str:
        slug = profile.slug.strip().lower()
        if not slug or len(slug) > 100:
            raise ValueError(
                "business profile slug must be between 1 and 100 characters"
            )
        return slug

    def _profile_row(self, organization_id: str, slug: str) -> dict[str, Any] | None:
        rows = self._store.query(
            "SELECT id, organization_id, slug, name, timezone, created_at, updated_at"
            " FROM business_profiles WHERE organization_id = ? AND slug = ?",
            (organization_id, slug),
        )
        return rows[0] if rows else None

    def _assigned_numbers(self, numbers: list[str]) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in numbers)
        return self._store.query(
            "SELECT e164, organization_id, business_profile_id FROM phone_numbers"
            f" WHERE e164 IN ({placeholders})",
            tuple(numbers),
        )

    @staticmethod
    def _check_number_ownership(
        assigned: list[dict[str, Any]], organization_id: str, profile_id: str
    ) -> None:
        for number in assigned:
            if (
                number["organization_id"] != organization_id
                or number["business_profile_id"] != profile_id
            ):
                raise PhoneNumberAlreadyAssigned(
                    f"phone number {number['e164']} is already assigned"
                )

    def save_draft(
        self, organization_id: str, profile: BusinessProfile
    ) -> BusinessProfile:
        """Save a validated configuration without changing live call routing."""
        if self._store.organization(organization_id) is None:
            raise ValueError("organization does not exist")

        slug = self._slug(profile)
        config, serialized = self._configuration(profile)
        business = config["business"]
        numbers = [normalize_e164(number) for number in business["phone_numbers"]]
        existing_profile = self._profile_row(organization_id, slug)
        profile_id = (
            str(existing_profile["id"])
            if existing_profile
            else self._profile_id(organization_id, slug)
        )
        self._check_number_ownership(
            self._assigned_numbers(numbers), organization_id, profile_id
        )

        draft = self._draft_row_for_slug(organization_id, slug)
        if draft is not None and draft["config"] == serialized:
            return self._from_row(draft)

        versions = self._store.query(
            "SELECT COALESCE(MAX(version_number), 0) AS latest"
            " FROM agent_versions WHERE business_profile_id = ?",
            (profile_id,),
        )
        version_number = int(versions[0]["latest"]) + 1
        version_id = str(uuid.uuid4())
        now = _now()
        candidate = BusinessProfile(raw=config, slug=slug)
        statements: list[tuple[str, tuple[Any, ...]]] = []
        if existing_profile:
            statements.append(
                (
                    (
                        "UPDATE business_profiles SET name = ?, timezone = ?,"
                        " updated_at = ? WHERE organization_id = ? AND id = ?"
                    ),
                    (
                        business["name"],
                        business["timezone"],
                        now,
                        organization_id,
                        profile_id,
                    ),
                )
            )
        else:
            statements.append(
                (
                    (
                        "INSERT INTO business_profiles"
                        " (id, organization_id, slug, name, timezone, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        profile_id,
                        organization_id,
                        slug,
                        business["name"],
                        business["timezone"],
                        now,
                        now,
                    ),
                )
            )
        statements.extend(
            [
                (
                    (
                        "UPDATE agent_versions SET status = 'archived'"
                        " WHERE organization_id = ? AND business_profile_id = ?"
                        " AND status = 'draft'"
                    ),
                    (organization_id, profile_id),
                ),
                (
                    (
                        "INSERT INTO agent_versions"
                        " (id, organization_id, business_profile_id, version_number,"
                        " status, config, rendered_prompt, created_at, published_at)"
                        " VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, NULL)"
                    ),
                    (
                        version_id,
                        organization_id,
                        profile_id,
                        version_number,
                        serialized,
                        render_instructions(candidate),
                        now,
                    ),
                ),
            ]
        )
        self._store.transaction(statements)
        row = self._draft_row_for_slug(organization_id, slug)
        if row is None or row["version_id"] != version_id:
            raise RuntimeError("draft business profile could not be reloaded")
        return self._from_row(row)

    def draft(self, organization_id: str, slug: str) -> BusinessProfile | None:
        row = self._draft_row_for_slug(organization_id, slug)
        return self._from_row(row) if row else None

    def draft_preview(self, organization_id: str, slug: str) -> dict[str, Any]:
        row = self._draft_row_for_slug(organization_id, slug)
        if row is None:
            raise DraftNotFound("No business profile draft exists.")
        # Revalidate at the publication boundary even if a row was written by an
        # older release or imported manually.
        PublishedBusinessConfiguration.model_validate(
            self._deserialize_config(row["config"])
        )
        return {
            "profile_id": str(row["profile_id"]),
            "version_id": str(row["version_id"]),
            "version_number": int(row["version_number"]),
            "rendered_prompt": str(row["rendered_prompt"]),
            "configuration": self._deserialize_config(row["config"]),
        }

    def profile_state(self, organization_id: str) -> dict[str, Any] | None:
        profiles = self._store.query(
            "SELECT id, slug, name, timezone FROM business_profiles"
            " WHERE organization_id = ? ORDER BY created_at, id LIMIT 1",
            (organization_id,),
        )
        if not profiles:
            return None
        profile = profiles[0]
        slug = str(profile["slug"])
        draft = self._draft_row_for_slug(organization_id, slug)
        published = self._published_row_for_slug(organization_id, slug)
        selected = draft or published
        active_phone_numbers = [
            str(row["e164"])
            for row in self._store.query(
                "SELECT e164 FROM phone_numbers"
                " WHERE organization_id = ? AND business_profile_id = ?"
                " AND status = 'active' ORDER BY e164",
                (organization_id, profile["id"]),
            )
        ]
        return {
            "id": str(profile["id"]),
            "slug": slug,
            "name": str(profile["name"]),
            "timezone": str(profile["timezone"]),
            "draft_version": int(draft["version_number"]) if draft else None,
            "published_version": (
                int(published["version_number"]) if published else None
            ),
            "active_phone_numbers": active_phone_numbers,
            "configuration": (
                self._deserialize_config(selected["config"]) if selected else None
            ),
        }

    def agent_overview(self, organization_id: str) -> dict[str, Any] | None:
        """The single agent an organization owns: its live version, its
        unpublished draft (if any), and the numbers currently routing to it.

        Returns ``None`` when the organization has no business profile yet
        (e.g. on-demand number provisioning failed during signup).
        """
        profiles = self._store.query(
            "SELECT id, slug, name FROM business_profiles"
            " WHERE organization_id = ? ORDER BY created_at, id LIMIT 1",
            (organization_id,),
        )
        if not profiles:
            return None
        row = profiles[0]
        slug = str(row["slug"])
        published = self._published_row_for_slug(organization_id, slug)
        draft = self._draft_row_for_slug(organization_id, slug)
        active_numbers = [
            str(r["e164"])
            for r in self._store.query(
                "SELECT e164 FROM phone_numbers WHERE organization_id = ?"
                " AND business_profile_id = ? AND status = 'active' ORDER BY e164",
                (organization_id, row["id"]),
            )
        ]

        def _view(version_row: dict[str, Any] | None) -> dict[str, Any] | None:
            if version_row is None:
                return None
            return {
                "version_number": int(version_row["version_number"]),
                "configuration": self._deserialize_config(version_row["config"]),
                "rendered_prompt": str(version_row["rendered_prompt"]),
            }

        return {
            "slug": slug,
            "name": str(row["name"]),
            "active_phone_numbers": active_numbers,
            "published": _view(published),
            "draft": _view(draft),
        }

    def discard_draft(self, organization_id: str, slug: str) -> bool:
        """Archive the current draft without touching the published version.
        Returns ``False`` when there was no draft to discard.
        """
        row = self._draft_row_for_slug(organization_id, slug)
        if row is None:
            return False
        self._store.execute(
            "UPDATE agent_versions SET status = 'archived'"
            " WHERE organization_id = ? AND business_profile_id = ? AND status = 'draft'",
            (organization_id, str(row["profile_id"])),
        )
        return True

    def publish(
        self, profile: BusinessProfile, organization_id: str | None = None
    ) -> BusinessProfile:
        slug = self._slug(profile)

        config, serialized = self._configuration(profile)
        business = config["business"]
        numbers = [normalize_e164(number) for number in business["phone_numbers"]]
        if organization_id is None:
            organization_id = self._store.ensure_organization(
                slug, str(business["name"])
            )
        elif self._store.organization(organization_id) is None:
            raise ValueError("organization does not exist")

        existing_profile = self._profile_row(organization_id, slug)
        profile_id = (
            str(existing_profile["id"])
            if existing_profile
            else self._profile_id(organization_id, slug)
        )

        assigned = self._assigned_numbers(numbers)
        self._check_number_ownership(assigned, organization_id, profile_id)

        published = self._published_row_for_slug(organization_id, slug)
        version_id: str
        statements: list[tuple[str, tuple[Any, ...]]] = []
        now = _now()

        if existing_profile:
            statements.append(
                (
                    (
                        "UPDATE business_profiles SET name = ?, timezone = ?,"
                        " updated_at = ? WHERE organization_id = ? AND id = ?"
                    ),
                    (
                        business["name"],
                        business["timezone"],
                        now,
                        organization_id,
                        profile_id,
                    ),
                )
            )
        else:
            statements.append(
                (
                    (
                        "INSERT INTO business_profiles"
                        " (id, organization_id, slug, name, timezone, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        profile_id,
                        organization_id,
                        slug,
                        business["name"],
                        business["timezone"],
                        now,
                        now,
                    ),
                )
            )

        if published and published["config"] == serialized:
            version_id = str(published["version_id"])
        else:
            versions = self._store.query(
                "SELECT COALESCE(MAX(version_number), 0) AS latest"
                " FROM agent_versions WHERE business_profile_id = ?",
                (profile_id,),
            )
            version_number = int(versions[0]["latest"]) + 1
            version_id = str(uuid.uuid4())
            candidate = BusinessProfile(raw=config, slug=slug)
            rendered_prompt = render_instructions(candidate)
            statements.extend(
                [
                    (
                        (
                            "UPDATE agent_versions SET status = 'archived'"
                            " WHERE organization_id = ? AND business_profile_id = ?"
                            " AND status IN ('published', 'draft')"
                        ),
                        (organization_id, profile_id),
                    ),
                    (
                        (
                            "INSERT INTO agent_versions"
                            " (id, organization_id, business_profile_id, version_number,"
                            " status, config, rendered_prompt, created_at, published_at)"
                            " VALUES (?, ?, ?, ?, 'published', ?, ?, ?, ?)"
                        ),
                        (
                            version_id,
                            organization_id,
                            profile_id,
                            version_number,
                            serialized,
                            rendered_prompt,
                            now,
                            now,
                        ),
                    ),
                ]
            )

        if published and published["config"] == serialized:
            statements.append(
                (
                    (
                        "UPDATE agent_versions SET status = 'archived'"
                        " WHERE organization_id = ? AND business_profile_id = ?"
                        " AND status = 'draft'"
                    ),
                    (organization_id, profile_id),
                )
            )

        statements.append(
            (
                (
                    "UPDATE phone_numbers SET status = 'inactive', updated_at = ?"
                    " WHERE organization_id = ? AND business_profile_id = ?"
                ),
                (now, organization_id, profile_id),
            )
        )
        assigned_by_number = {str(row["e164"]): row for row in assigned}
        for number in numbers:
            if number in assigned_by_number:
                statements.append(
                    (
                        (
                            "UPDATE phone_numbers SET status = 'active', updated_at = ?"
                            " WHERE organization_id = ? AND business_profile_id = ?"
                            " AND e164 = ?"
                        ),
                        (now, organization_id, profile_id, number),
                    )
                )
            else:
                statements.append(
                    (
                        (
                            "INSERT INTO phone_numbers"
                            " (id, organization_id, business_profile_id, e164, status,"
                            " created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)"
                        ),
                        (
                            self._phone_id(number),
                            organization_id,
                            profile_id,
                            number,
                            now,
                            now,
                        ),
                    )
                )

        self._store.transaction(statements)
        row = self._published_row_for_slug(organization_id, slug)
        if row is None or row["version_id"] != version_id:
            raise RuntimeError("published business profile could not be reloaded")
        return self._from_row(row)

    def publish_draft(self, organization_id: str, slug: str) -> BusinessProfile:
        row = self._draft_row_for_slug(organization_id, slug)
        if row is None:
            raise DraftNotFound("No business profile draft exists.")
        profile = self._from_row(row)
        config, _serialized = self._configuration(profile)
        numbers = [
            normalize_e164(number) for number in config["business"]["phone_numbers"]
        ]
        profile_id = str(row["profile_id"])
        assigned = self._assigned_numbers(numbers)
        self._check_number_ownership(assigned, organization_id, profile_id)
        now = _now()
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                (
                    "UPDATE business_profiles SET name = ?, timezone = ?,"
                    " updated_at = ? WHERE organization_id = ? AND id = ?"
                ),
                (
                    config["business"]["name"],
                    config["business"]["timezone"],
                    now,
                    organization_id,
                    profile_id,
                ),
            ),
            (
                (
                    "UPDATE agent_versions SET status = 'archived'"
                    " WHERE organization_id = ? AND business_profile_id = ?"
                    " AND status = 'published'"
                ),
                (organization_id, profile_id),
            ),
            (
                (
                    "UPDATE agent_versions SET status = 'published', published_at = ?"
                    " WHERE organization_id = ? AND business_profile_id = ?"
                    " AND id = ? AND status = 'draft'"
                ),
                (now, organization_id, profile_id, row["version_id"]),
            ),
            (
                (
                    "UPDATE phone_numbers SET status = 'inactive', updated_at = ?"
                    " WHERE organization_id = ? AND business_profile_id = ?"
                ),
                (now, organization_id, profile_id),
            ),
        ]
        assigned_by_number = {str(item["e164"]): item for item in assigned}
        for number in numbers:
            if number in assigned_by_number:
                statements.append(
                    (
                        (
                            "UPDATE phone_numbers SET status = 'active', updated_at = ?"
                            " WHERE organization_id = ? AND business_profile_id = ?"
                            " AND e164 = ?"
                        ),
                        (now, organization_id, profile_id, number),
                    )
                )
            else:
                statements.append(
                    (
                        (
                            "INSERT INTO phone_numbers"
                            " (id, organization_id, business_profile_id, e164, status,"
                            " created_at, updated_at)"
                            " VALUES (?, ?, ?, ?, 'active', ?, ?)"
                        ),
                        (
                            self._phone_id(number),
                            organization_id,
                            profile_id,
                            number,
                            now,
                            now,
                        ),
                    )
                )
        self._store.transaction(statements)
        published = self._published_row_for_slug(organization_id, slug)
        if published is None or published["version_id"] != row["version_id"]:
            raise RuntimeError("draft business profile could not be published")
        return self._from_row(published)

    def find_by_phone_number(self, called_number: str) -> BusinessProfile | None:
        try:
            e164 = normalize_e164(called_number)
        except ValueError:
            e164 = ""
        if e164:
            rows = self._store.query(
                "SELECT business_profiles.id AS profile_id,"
                " business_profiles.organization_id, business_profiles.slug,"
                " agent_versions.id AS version_id, agent_versions.version_number,"
                " agent_versions.config FROM phone_numbers"
                " JOIN business_profiles"
                " ON business_profiles.id = phone_numbers.business_profile_id"
                " AND business_profiles.organization_id = phone_numbers.organization_id"
                " JOIN agent_versions"
                " ON agent_versions.business_profile_id = business_profiles.id"
                " AND agent_versions.organization_id = business_profiles.organization_id"
                " WHERE phone_numbers.e164 = ? AND phone_numbers.status = 'active'"
                " AND agent_versions.status = 'published'"
                " ORDER BY agent_versions.version_number DESC LIMIT 1",
                (e164,),
            )
            if rows:
                return self._from_row(rows[0])

        return None

    def list_published(self) -> list[BusinessProfile]:
        rows = self._store.query(
            "SELECT business_profiles.id AS profile_id,"
            " business_profiles.organization_id, business_profiles.slug,"
            " agent_versions.id AS version_id, agent_versions.version_number,"
            " agent_versions.config FROM business_profiles"
            " JOIN agent_versions"
            " ON agent_versions.business_profile_id = business_profiles.id"
            " AND agent_versions.organization_id = business_profiles.organization_id"
            " WHERE agent_versions.status = 'published'"
            " ORDER BY business_profiles.slug, agent_versions.version_number DESC"
        )
        profiles: list[BusinessProfile] = []
        seen: set[str] = set()
        for row in rows:
            profile_id = str(row["profile_id"])
            if profile_id in seen:
                continue
            seen.add(profile_id)
            profiles.append(self._from_row(row))
        return profiles

    def export_yaml(self, organization_id: str, slug: str) -> str | None:
        row = self._published_row_for_slug(organization_id, slug)
        if row is None:
            return None
        return yaml.safe_dump(
            self._deserialize_config(row["config"]),
            allow_unicode=True,
            sort_keys=False,
        )
