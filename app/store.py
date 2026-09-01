from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POSTGRES_PREFIXES = ("postgres://", "postgresql://")

# A new call row wins over any earlier row with the same id. SQLite's REPLACE
# used to drop the old row wholesale. The tenant-aware schema has child foreign
# keys, so both backends now use an in-place upsert instead.
START_CALL = (
    "INSERT INTO calls"
    " (call_id, organization_id, agent_version_id, business, from_number,"
    " to_number, started_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (call_id) DO UPDATE SET"
    " agent_version_id = excluded.agent_version_id, business = excluded.business,"
    " from_number = excluded.from_number,"
    " to_number = EXCLUDED.to_number, started_at = EXCLUDED.started_at,"
    " ended_at = NULL, outcome = NULL, summary = NULL, model_cost = 0,"
    " transcript_deleted_at = NULL"
    " WHERE calls.organization_id = excluded.organization_id"
)


class TenantScopeError(ValueError):
    """A caller attempted to attach one tenant's record to another tenant."""


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def is_postgres_url(target: str | Path) -> bool:
    return isinstance(target, str) and target.startswith(POSTGRES_PREFIXES)


class _Backend(ABC):
    """Runs the handful of statements the app needs, in one SQL dialect.

    Queries are written with `?` placeholders throughout; a backend rewrites
    them if its driver expects something else.
    """

    @abstractmethod
    def execute(self, sql: str, params: tuple[Any, ...]) -> None: ...

    @abstractmethod
    def execute_returning(
        self, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def transaction(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class _SqliteBackend(_Backend):
    """Writes are tiny and infrequent (a few per call), so a single
    lock-guarded connection is simpler than pulling in an async driver.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def execute_returning(
        self, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        # One write plus its RETURNING rows, under a single lock hold so a
        # concurrent writer cannot slip between the write and the read.
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            self._conn.commit()
        return [dict(row) for row in rows]

    def query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def transaction(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        with self._lock:
            try:
                for sql, params in statements:
                    self._conn.execute(sql, params)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _PostgresBackend(_Backend):
    """Pooled Postgres access.

    A pool rather than one long-lived connection because managed Postgres
    recycles idle connections, and a dropped socket would otherwise surface
    mid-call.
    """

    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 4):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
            timeout=15,
        )
        self._pool.wait(timeout=30)

    @staticmethod
    def _adapt(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._pool.connection() as conn:
            conn.execute(self._adapt(sql), params)

    def execute_returning(
        self, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            return list(conn.execute(self._adapt(sql), params).fetchall())

    def query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            return list(conn.execute(self._adapt(sql), params).fetchall())

    def transaction(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        with self._pool.connection() as conn, conn.transaction():
            for sql, params in statements:
                conn.execute(self._adapt(sql), params)

    def close(self) -> None:
        self._pool.close()


class Store:
    """Tenant and call data, backed by Postgres or a local SQLite database."""

    def __init__(self, target: str | Path, *, migrate: bool = True):
        if migrate:
            # Imports here avoid making Alembic part of this module's import graph.
            from .migrations import upgrade_database

            upgrade_database(target)
        if is_postgres_url(target):
            self._backend: _Backend = _PostgresBackend(str(target))
            self.dialect = "postgres"
        else:
            self._backend = _SqliteBackend(Path(target))
            self.dialect = "sqlite"

    def close(self) -> None:
        self._backend.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._backend.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self._backend.query(sql, params)

    def execute_returning(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        return self._backend.execute_returning(sql, params)

    def transaction(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._backend.transaction(statements)

    def ensure_organization(self, slug: str, name: str) -> str:
        slug = slug.strip().lower()
        name = name.strip()
        if not slug or not name:
            raise ValueError("organization slug and name are required")

        organization_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"call-agent:organization:{slug}")
        )
        self._backend.execute(
            "INSERT INTO organizations (id, slug, name, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (slug) DO UPDATE SET"
            " name = excluded.name, updated_at = excluded.updated_at",
            (organization_id, slug, name, _now()),
        )
        rows = self._backend.query(
            "SELECT id FROM organizations WHERE slug = ?", (slug,)
        )
        return str(rows[0]["id"])

    def organization_id_for_slug(self, slug: str) -> str | None:
        rows = self._backend.query(
            "SELECT id FROM organizations WHERE slug = ? AND deleted_at IS NULL",
            (slug.strip().lower(),),
        )
        return str(rows[0]["id"]) if rows else None

    def create_user(self, email: str, password_hash: str | None = None) -> str:
        email = email.strip().lower()
        if not email:
            raise ValueError("email is required")
        user_id = str(uuid.uuid4())
        self._backend.execute(
            "INSERT INTO users (id, email, password_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (email) DO NOTHING",
            (user_id, email, password_hash, _now(), _now()),
        )
        rows = self._backend.query("SELECT id FROM users WHERE email = ?", (email,))
        return str(rows[0]["id"])

    _USER_COLUMNS = (
        "id, email, password_hash, email_verified_at, is_platform_admin,"
        " created_at, updated_at"
    )

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._USER_COLUMNS} FROM users WHERE email = ?",
            (email.strip().lower(),),
        )
        return rows[0] if rows else None

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
        )
        return rows[0] if rows else None

    def set_user_password(self, user_id: str, password_hash: str) -> None:
        self._backend.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, _now(), user_id),
        )

    def mark_email_verified(self, user_id: str) -> None:
        now = _now()
        self._backend.execute(
            "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?),"
            " updated_at = ? WHERE id = ?",
            (now, now, user_id),
        )

    def create_organization(self, slug: str, name: str) -> str:
        slug = slug.strip().lower()
        name = name.strip()
        if not slug or not name:
            raise ValueError("organization slug and name are required")
        organization_id = str(uuid.uuid4())
        now = _now()
        self._backend.execute(
            "INSERT INTO organizations (id, slug, name, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (organization_id, slug, name, now, now),
        )
        return organization_id

    def organization(self, organization_id: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            "SELECT id, slug, name, lifecycle, created_at, updated_at, deleted_at"
            " FROM organizations WHERE id = ? AND deleted_at IS NULL",
            (organization_id,),
        )
        return rows[0] if rows else None

    def set_organization_lifecycle(
        self, organization_id: str, lifecycle: str
    ) -> None:
        self._backend.execute(
            "UPDATE organizations SET lifecycle = ?, updated_at = ?"
            " WHERE id = ? AND deleted_at IS NULL",
            (lifecycle, _now(), organization_id),
        )

    def membership_role(self, organization_id: str, user_id: str) -> str | None:
        rows = self._backend.query(
            "SELECT role FROM memberships WHERE organization_id = ? AND user_id = ?",
            (organization_id, user_id),
        )
        return str(rows[0]["role"]) if rows else None

    def add_membership(self, organization_id: str, user_id: str, role: str) -> None:
        allowed_roles = {"owner", "admin", "member", "viewer"}
        if role not in allowed_roles:
            raise ValueError(f"unknown membership role: {role}")
        self._backend.execute(
            "INSERT INTO memberships"
            " (organization_id, user_id, role, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (organization_id, user_id) DO UPDATE SET"
            " role = excluded.role, updated_at = excluded.updated_at",
            (organization_id, user_id, role, _now(), _now()),
        )

    def organizations_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return self._backend.query(
            "SELECT organizations.id, organizations.slug, organizations.name,"
            " memberships.role FROM memberships"
            " JOIN organizations"
            " ON organizations.id = memberships.organization_id"
            " WHERE memberships.user_id = ? AND organizations.deleted_at IS NULL"
            " ORDER BY organizations.name",
            (user_id,),
        )

    def start_call(
        self,
        organization_id: str,
        call_id: str,
        business: str,
        from_number: str,
        to_number: str,
        agent_version_id: str | None = None,
    ) -> None:
        self._backend.execute(
            START_CALL,
            (
                call_id,
                organization_id,
                agent_version_id,
                business,
                from_number,
                to_number,
                _now(),
            ),
        )
        rows = self._backend.query(
            "SELECT organization_id FROM calls WHERE call_id = ?", (call_id,)
        )
        if rows and rows[0]["organization_id"] != organization_id:
            raise TenantScopeError(
                f"call {call_id!r} already belongs to another organization"
            )

    def add_turn(
        self, organization_id: str, call_id: str, role: str, text: str
    ) -> None:
        self._backend.execute(
            "INSERT INTO turns (organization_id, call_id, role, text, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (organization_id, call_id, role, text, _now()),
        )

    def add_lead(self, organization_id: str, call_id: str, lead: dict[str, Any]) -> int:
        rows = self._backend.execute_returning(
            "INSERT INTO leads (organization_id, call_id, intent, caller_name,"
            " callback_number, urgency, preferred_time, details, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                organization_id,
                call_id,
                lead.get("intent"),
                lead.get("caller_name"),
                lead.get("callback_number"),
                lead.get("urgency"),
                lead.get("preferred_time"),
                lead.get("details"),
                _now(),
            ),
        )
        return int(rows[0]["id"])

    def finish_call(
        self,
        organization_id: str,
        call_id: str,
        outcome: str,
        summary: str,
        model_cost: float,
        usage_events: list[dict[str, Any]] | None = None,
    ) -> None:
        from app.domains.billing.usage import insert_statement

        ended_at = _now()
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                (
                    "UPDATE calls SET ended_at = ?, outcome = ?, summary = ?,"
                    " model_cost = ? WHERE organization_id = ? AND call_id = ?"
                ),
                (
                    ended_at,
                    outcome,
                    summary,
                    round(model_cost, 6),
                    organization_id,
                    call_id,
                ),
            )
        ]
        statements.extend(insert_statement(event) for event in usage_events or [])
        self._backend.transaction(statements)

    def transcript(self, organization_id: str, call_id: str) -> list[dict[str, Any]]:
        return self._backend.query(
            "SELECT role, text, at FROM turns"
            " WHERE organization_id = ? AND call_id = ? ORDER BY id",
            (organization_id, call_id),
        )

    def recent_calls(
        self, organization_id: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        calls = self._backend.query(
            "SELECT * FROM calls WHERE organization_id = ?"
            " ORDER BY started_at DESC LIMIT ?",
            (organization_id, limit),
        )
        for call in calls:
            call["leads"] = self._backend.query(
                "SELECT intent, caller_name, callback_number, urgency, preferred_time,"
                " details, status FROM leads WHERE organization_id = ? AND call_id = ?",
                (organization_id, call["call_id"]),
            )
        return calls

    def call_detail(self, organization_id: str, call_id: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            "SELECT * FROM calls WHERE organization_id = ? AND call_id = ?",
            (organization_id, call_id),
        )
        if not rows:
            return None
        detail = rows[0]
        detail["leads"] = self._backend.query(
            "SELECT intent, caller_name, callback_number, urgency, preferred_time,"
            " details, status FROM leads WHERE organization_id = ? AND call_id = ?",
            (organization_id, call_id),
        )
        detail["transcript"] = self.transcript(organization_id, call_id)
        return detail

    # -- management API pagination -------------------------------------------

    def calls_page(
        self,
        organization_id: str,
        limit: int,
        before_started_at: str | None = None,
        before_call_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of calls, newest first, keyed on (started_at, call_id).

        ``before_started_at`` is the ISO string carried in the opaque cursor;
        Postgres needs it back as a real timestamp for the comparison, SQLite
        compares the stored ISO text directly.
        """
        sql = "SELECT * FROM calls WHERE organization_id = ?"
        params: list[Any] = [organization_id]
        if before_started_at is not None and before_call_id is not None:
            cursor_ts: Any = before_started_at
            if self.dialect == "postgres":
                cursor_ts = datetime.fromisoformat(before_started_at)
            sql += " AND (started_at < ? OR (started_at = ? AND call_id < ?))"
            params += [cursor_ts, cursor_ts, before_call_id]
        sql += " ORDER BY started_at DESC, call_id DESC LIMIT ?"
        params.append(limit)
        calls = self._backend.query(sql, tuple(params))
        for call in calls:
            call["leads"] = self._backend.query(
                "SELECT intent, caller_name, callback_number, urgency, preferred_time,"
                " details, status FROM leads WHERE organization_id = ? AND call_id = ?",
                (organization_id, call["call_id"]),
            )
        return calls

    def leads_page(
        self,
        organization_id: str,
        limit: int,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """One page of captured leads, newest first, keyed on the row id."""
        sql = (
            "SELECT id, call_id, intent, caller_name, callback_number, urgency,"
            " preferred_time, details, at, status, status_note, status_updated_at"
            " FROM leads WHERE organization_id = ?"
        )
        params: list[Any] = [organization_id]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._backend.query(sql, tuple(params))

    def lead(self, organization_id: str, lead_id: int) -> dict[str, Any] | None:
        rows = self._backend.query(
            "SELECT id, call_id, intent, caller_name, callback_number, urgency,"
            " preferred_time, details, at, status, status_note, status_updated_at,"
            " status_updated_by FROM leads WHERE organization_id = ? AND id = ?",
            (organization_id, lead_id),
        )
        return rows[0] if rows else None

    def update_lead_status(
        self,
        organization_id: str,
        lead_id: int,
        *,
        status: str,
        note: str | None,
        updated_by: str,
    ) -> dict[str, Any] | None:
        rows = self._backend.execute_returning(
            "UPDATE leads SET status = ?, status_note = ?, status_updated_at = ?,"
            " status_updated_by = ? WHERE organization_id = ? AND id = ?"
            " RETURNING id",
            (status, note, _now(), updated_by, organization_id, lead_id),
        )
        if not rows:
            return None
        return self.lead(organization_id, lead_id)

    # -- browser sessions ---------------------------------------------------

    def create_session(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> str:
        session_id = str(uuid.uuid4())
        self._backend.execute(
            "INSERT INTO user_sessions"
            " (id, user_id, token_hash, user_agent, ip, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                user_id,
                token_hash,
                (user_agent or "")[:400] or None,
                ip,
                _now(),
                expires_at,
            ),
        )
        return session_id

    def active_session(self, token_hash: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            "SELECT user_sessions.id AS session_id, user_sessions.expires_at,"
            " users.id AS user_id, users.email, users.email_verified_at,"
            " users.is_platform_admin FROM user_sessions"
            " JOIN users ON users.id = user_sessions.user_id"
            " WHERE user_sessions.token_hash = ?"
            " AND user_sessions.revoked_at IS NULL"
            " AND user_sessions.expires_at > ?",
            (token_hash, _now()),
        )
        return rows[0] if rows else None

    def revoke_session(self, token_hash: str) -> None:
        self._backend.execute(
            "UPDATE user_sessions SET revoked_at = ?"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (_now(), token_hash),
        )

    def revoke_all_user_sessions(self, user_id: str) -> None:
        self._backend.execute(
            "UPDATE user_sessions SET revoked_at = ?"
            " WHERE user_id = ? AND revoked_at IS NULL",
            (_now(), user_id),
        )

    # -- email verification / password reset tokens -----------------------

    def create_email_token(
        self, user_id: str, purpose: str, token_hash: str, expires_at: datetime
    ) -> str:
        token_id = str(uuid.uuid4())
        self._backend.execute(
            "INSERT INTO email_tokens"
            " (id, user_id, purpose, token_hash, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, user_id, purpose, token_hash, _now(), expires_at),
        )
        return token_id

    def consume_email_token(self, purpose: str, token_hash: str) -> str | None:
        """Return the token's user id and mark it used, or None if unusable."""
        now = _now()
        rows = self._backend.query(
            "SELECT id, user_id FROM email_tokens"
            " WHERE purpose = ? AND token_hash = ? AND consumed_at IS NULL"
            " AND expires_at > ?",
            (purpose, token_hash, now),
        )
        if not rows:
            return None
        self._backend.execute(
            "UPDATE email_tokens SET consumed_at = ?"
            " WHERE id = ? AND consumed_at IS NULL",
            (now, rows[0]["id"]),
        )
        return str(rows[0]["user_id"])

    # -- organization profile & members ----------------------------------

    def update_organization(self, organization_id: str, name: str) -> None:
        self._backend.execute(
            "UPDATE organizations SET name = ?, updated_at = ? WHERE id = ?",
            (name.strip(), _now(), organization_id),
        )

    def list_members(self, organization_id: str) -> list[dict[str, Any]]:
        return self._backend.query(
            "SELECT users.id AS user_id, users.email, memberships.role,"
            " memberships.created_at AS joined_at,"
            " users.email_verified_at FROM memberships"
            " JOIN users ON users.id = memberships.user_id"
            " WHERE memberships.organization_id = ?"
            " ORDER BY memberships.created_at, users.email",
            (organization_id,),
        )

    def count_role(self, organization_id: str, role: str) -> int:
        rows = self._backend.query(
            "SELECT COUNT(*) AS n FROM memberships"
            " WHERE organization_id = ? AND role = ?",
            (organization_id, role),
        )
        return int(rows[0]["n"])

    def set_membership_role(
        self, organization_id: str, user_id: str, role: str
    ) -> None:
        self._backend.execute(
            "UPDATE memberships SET role = ?, updated_at = ?"
            " WHERE organization_id = ? AND user_id = ?",
            (role, _now(), organization_id, user_id),
        )

    def remove_membership(self, organization_id: str, user_id: str) -> None:
        self._backend.execute(
            "DELETE FROM memberships WHERE organization_id = ? AND user_id = ?",
            (organization_id, user_id),
        )

    # -- invitations ---------------------------------------------------

    _INVITATION_COLUMNS = (
        "id, organization_id, email, role, invited_by,"
        " created_at, expires_at, accepted_at"
    )

    def create_invitation(
        self,
        organization_id: str,
        email: str,
        role: str,
        token_hash: str,
        invited_by: str | None,
        expires_at: datetime,
    ) -> str:
        invitation_id = str(uuid.uuid4())
        self._backend.execute(
            "INSERT INTO invitations"
            " (id, organization_id, email, role, token_hash, invited_by,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invitation_id,
                organization_id,
                email.strip().lower(),
                role,
                token_hash,
                invited_by,
                _now(),
                expires_at,
            ),
        )
        return invitation_id

    def invitation_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._INVITATION_COLUMNS}, token_hash,"
            " (SELECT name FROM organizations WHERE id = invitations.organization_id)"
            " AS organization_name FROM invitations WHERE token_hash = ?",
            (token_hash,),
        )
        return rows[0] if rows else None

    def pending_invitation_for(
        self, organization_id: str, email: str
    ) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._INVITATION_COLUMNS} FROM invitations"
            " WHERE organization_id = ? AND email = ? AND accepted_at IS NULL",
            (organization_id, email.strip().lower()),
        )
        return rows[0] if rows else None

    def list_pending_invitations(self, organization_id: str) -> list[dict[str, Any]]:
        return self._backend.query(
            f"SELECT {self._INVITATION_COLUMNS} FROM invitations"
            " WHERE organization_id = ? AND accepted_at IS NULL"
            " ORDER BY created_at DESC",
            (organization_id,),
        )

    def get_invitation(
        self, organization_id: str, invitation_id: str
    ) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._INVITATION_COLUMNS} FROM invitations"
            " WHERE organization_id = ? AND id = ?",
            (organization_id, invitation_id),
        )
        return rows[0] if rows else None

    def delete_invitation(self, organization_id: str, invitation_id: str) -> None:
        self._backend.execute(
            "DELETE FROM invitations WHERE organization_id = ? AND id = ?",
            (organization_id, invitation_id),
        )

    def mark_invitation_accepted(self, invitation_id: str) -> None:
        self._backend.execute(
            "UPDATE invitations SET accepted_at = ?"
            " WHERE id = ? AND accepted_at IS NULL",
            (_now(), invitation_id),
        )

    # -- audit log ---------------------------------------------------

    def record_audit(
        self,
        action: str,
        *,
        organization_id: str | None = None,
        actor_user_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        self._backend.execute(
            "INSERT INTO audit_logs"
            " (organization_id, actor_user_id, action, target_type, target_id,"
            " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                organization_id,
                actor_user_id,
                action,
                target_type,
                target_id,
                json.dumps(metadata, default=str) if metadata else None,
                ip,
                _now(),
            ),
        )

    def audit_log_page(
        self, organization_id: str, limit: int, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, organization_id, actor_user_id, action, target_type,"
            " target_id, metadata, ip, created_at FROM audit_logs"
            " WHERE organization_id = ?"
        )
        params: list[Any] = [organization_id]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._backend.query(sql, tuple(params))
        for row in rows:
            raw = row.get("metadata")
            row["metadata"] = json.loads(raw) if raw else None
        return rows

    # -- platform administration -----------------------------------

    def organizations_page(
        self,
        limit: int,
        before_created_at: str | None = None,
        before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT organizations.id, organizations.slug, organizations.name,"
            " organizations.lifecycle, organizations.created_at,"
            " (SELECT COUNT(*) FROM memberships"
            " WHERE memberships.organization_id = organizations.id)"
            " AS member_count,"
            " (SELECT status FROM subscriptions"
            " WHERE subscriptions.organization_id = organizations.id)"
            " AS subscription_status,"
            " (SELECT e164 FROM phone_numbers"
            " WHERE phone_numbers.organization_id = organizations.id"
            " AND phone_numbers.status = 'active' LIMIT 1) AS phone_number"
            " FROM organizations"
            " WHERE organizations.deleted_at IS NULL"
        )
        params: list[Any] = []
        if before_created_at is not None and before_id is not None:
            cursor_ts: Any = before_created_at
            if self.dialect == "postgres":
                cursor_ts = datetime.fromisoformat(before_created_at)
            sql += (
                " AND (organizations.created_at < ?"
                " OR (organizations.created_at = ? AND organizations.id < ?))"
            )
            params += [cursor_ts, cursor_ts, before_id]
        sql += " ORDER BY organizations.created_at DESC, organizations.id DESC LIMIT ?"
        params.append(limit)
        return self._backend.query(sql, tuple(params))

    def platform_stats(self, period_start: datetime) -> dict[str, Any]:
        """Read-only platform-operator overview: organization lifecycle counts,
        subscription mix, recent payment failures, this-period call volume/cost,
        and number-pool health.
        """
        lifecycle_rows = self._backend.query(
            "SELECT lifecycle, COUNT(*) AS n FROM organizations"
            " WHERE deleted_at IS NULL GROUP BY lifecycle",
            (),
        )
        subscription_rows = self._backend.query(
            "SELECT status, COUNT(*) AS n FROM subscriptions GROUP BY status", ()
        )
        failed_payments = self._backend.query(
            "SELECT COUNT(*) AS n FROM subscriptions"
            " WHERE last_invoice_status = 'payment_failed'",
            (),
        )
        calls = self._backend.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(model_cost), 0) AS cost"
            " FROM calls WHERE started_at >= ?",
            (period_start,),
        )
        spend = self._backend.query(
            "SELECT COALESCE(SUM(customer_charge_micros), 0) AS micros"
            " FROM usage_events WHERE occurred_at >= ?",
            (period_start,),
        )
        pool_rows = self._backend.query(
            "SELECT status, COUNT(*) AS n FROM phone_number_pool GROUP BY status",
            (),
        )
        return {
            "organizations": {
                str(r["lifecycle"]): int(r["n"]) for r in lifecycle_rows
            },
            "subscriptions": {
                str(r["status"]): int(r["n"]) for r in subscription_rows
            },
            "payment_failures": int(failed_payments[0]["n"]) if failed_payments else 0,
            "period_calls": int(calls[0]["n"]) if calls else 0,
            "period_model_cost_usd": round(float(calls[0]["cost"] or 0), 4)
            if calls
            else 0.0,
            "period_customer_charge_micros": int(spend[0]["micros"]) if spend else 0,
            "number_pool": {str(r["status"]): int(r["n"]) for r in pool_rows},
        }

    def set_platform_admin(self, user_id: str, value: bool) -> None:
        self._backend.execute(
            "UPDATE users SET is_platform_admin = ?, updated_at = ? WHERE id = ?",
            (1 if value else 0, _now(), user_id),
        )

    # -- pre-warmed number pool -----------------------------------------

    _POOL_COLUMNS = (
        "id, e164, country_code, provider, provider_number_sid,"
        " provider_trunk_sid, status, assigned_organization_id, assigned_at,"
        " quarantined_until, created_at, updated_at"
    )

    def add_pool_number(
        self,
        e164: str,
        country_code: str,
        *,
        provider_number_sid: str | None = None,
        provider_trunk_sid: str | None = None,
        provider: str = "twilio",
    ) -> bool:
        """Register a bought-and-trunked number as available. Idempotent on
        ``e164``; returns True only when a new row was inserted.
        """
        rows = self._backend.execute_returning(
            "INSERT INTO phone_number_pool"
            " (id, e164, country_code, provider, provider_number_sid,"
            " provider_trunk_sid, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'available', ?, ?)"
            " ON CONFLICT (e164) DO NOTHING RETURNING id",
            (
                str(uuid.uuid4()),
                e164,
                country_code.upper(),
                provider,
                provider_number_sid,
                provider_trunk_sid,
                _now(),
                _now(),
            ),
        )
        return bool(rows)

    def pool_counts(self) -> dict[str, int]:
        rows = self._backend.query(
            "SELECT status, COUNT(*) AS n FROM phone_number_pool GROUP BY status",
            (),
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    def available_pool_count(self) -> int:
        rows = self._backend.query(
            "SELECT COUNT(*) AS n FROM phone_number_pool WHERE status = 'available'",
            (),
        )
        return int(rows[0]["n"]) if rows else 0

    def claim_pool_number(
        self, organization_id: str, *, country_code: str | None = None
    ) -> dict[str, Any] | None:
        """Atomically hand one available number to an organization.

        A single conditional UPDATE so two concurrent signups cannot claim the
        same row; ``FOR UPDATE SKIP LOCKED`` on Postgres for multi-replica safety.
        ``country_code`` restricts the pick to that country so a leftover number
        from an old pool country is never handed to a new tenant.
        """
        now = _now()
        params: list[Any] = []
        where = "status = 'available'"
        if country_code:
            where += " AND country_code = ?"
            params.append(country_code.upper())
        pick = (
            f"SELECT id FROM phone_number_pool WHERE {where}"
            " ORDER BY created_at, id LIMIT 1"
        )
        if self.dialect == "postgres":
            pick += " FOR UPDATE SKIP LOCKED"
        rows = self._backend.execute_returning(
            "UPDATE phone_number_pool SET status = 'assigned',"
            " assigned_organization_id = ?, assigned_at = ?, updated_at = ?,"
            f" quarantined_until = NULL WHERE id IN ({pick})"
            f" RETURNING {self._POOL_COLUMNS}",
            (organization_id, now, now, *params),
        )
        return rows[0] if rows else None

    def pool_number_for_org(self, organization_id: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._POOL_COLUMNS} FROM phone_number_pool"
            " WHERE assigned_organization_id = ? AND status = 'assigned'"
            " ORDER BY assigned_at DESC LIMIT 1",
            (organization_id,),
        )
        return rows[0] if rows else None

    def release_pool_number(
        self, e164: str, *, quarantine_until: datetime | None
    ) -> None:
        """Return a number to the pool. ``quarantine_until`` set => it was live
        on a real call and must not be re-offered until then.
        """
        status = "quarantined" if quarantine_until is not None else "available"
        self._backend.execute(
            "UPDATE phone_number_pool SET status = ?, assigned_organization_id = NULL,"
            " assigned_at = NULL, quarantined_until = ?, updated_at = ?"
            " WHERE e164 = ?",
            (status, quarantine_until, _now(), e164),
        )

    def retire_available_pool_numbers(
        self, *, exclude_country: str
    ) -> list[dict[str, Any]]:
        """Retire every ``available`` number whose country is not ``exclude_country``.

        Used when the pool country changes: leftover numbers from the old country
        must stop being handed out. ``assigned`` numbers are left alone — a tenant
        is using them. Returns the retired rows so the caller can release them
        with the provider.
        """
        rows = self._backend.execute_returning(
            "UPDATE phone_number_pool SET status = 'retired',"
            " updated_at = ? WHERE status = 'available' AND country_code <> ?"
            f" RETURNING {self._POOL_COLUMNS}",
            (_now(), exclude_country.upper()),
        )
        return rows

    def promote_quarantined_pool_numbers(self) -> int:
        rows = self._backend.execute_returning(
            "UPDATE phone_number_pool SET status = 'available',"
            " quarantined_until = NULL, updated_at = ?"
            " WHERE status = 'quarantined' AND quarantined_until IS NOT NULL"
            " AND quarantined_until <= ? RETURNING id",
            (_now(), _now()),
        )
        return len(rows)

    # -- outbound webhooks ------------------------------------------------

    _ENDPOINT_COLUMNS = (
        "id, organization_id, url, description, event_types, active,"
        " created_at, updated_at"
    )

    def create_webhook_endpoint(
        self,
        organization_id: str,
        url: str,
        secret: str,
        description: str | None,
        event_types: str | None,
        active: bool,
    ) -> str:
        endpoint_id = str(uuid.uuid4())
        now = _now()
        self._backend.execute(
            "INSERT INTO webhook_endpoints"
            " (id, organization_id, url, secret, description, event_types, active,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id,
                organization_id,
                url,
                secret,
                description,
                event_types,
                1 if active else 0,
                now,
                now,
            ),
        )
        return endpoint_id

    def webhook_endpoint(
        self, organization_id: str, endpoint_id: str
    ) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._ENDPOINT_COLUMNS} FROM webhook_endpoints"
            " WHERE organization_id = ? AND id = ?",
            (organization_id, endpoint_id),
        )
        return rows[0] if rows else None

    def webhook_endpoint_secret(
        self, organization_id: str, endpoint_id: str
    ) -> str | None:
        rows = self._backend.query(
            "SELECT secret FROM webhook_endpoints WHERE organization_id = ? AND id = ?",
            (organization_id, endpoint_id),
        )
        return str(rows[0]["secret"]) if rows else None

    def list_webhook_endpoints(self, organization_id: str) -> list[dict[str, Any]]:
        return self._backend.query(
            f"SELECT {self._ENDPOINT_COLUMNS} FROM webhook_endpoints"
            " WHERE organization_id = ? ORDER BY created_at, id",
            (organization_id,),
        )

    def update_webhook_endpoint(
        self, organization_id: str, endpoint_id: str, fields: dict[str, Any]
    ) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name} = ?" for name in fields)
        params = list(fields.values())
        params += [_now(), organization_id, endpoint_id]
        self._backend.execute(
            f"UPDATE webhook_endpoints SET {columns}, updated_at = ?"
            " WHERE organization_id = ? AND id = ?",
            tuple(params),
        )

    def rotate_webhook_secret(
        self, organization_id: str, endpoint_id: str, secret: str
    ) -> None:
        self._backend.execute(
            "UPDATE webhook_endpoints SET secret = ?, updated_at = ?"
            " WHERE organization_id = ? AND id = ?",
            (secret, _now(), organization_id, endpoint_id),
        )

    def delete_webhook_endpoint(self, organization_id: str, endpoint_id: str) -> None:
        self._backend.execute(
            "DELETE FROM webhook_endpoints WHERE organization_id = ? AND id = ?",
            (organization_id, endpoint_id),
        )

    def enqueue_webhook_event(
        self,
        organization_id: str,
        event_type: str,
        dedupe_key: str,
        payload: str,
        occurred_at: datetime,
        max_attempts: int,
    ) -> str | None:
        """Create the event plus one pending delivery per matching active
        endpoint, atomically. Returns the event id, or None when the org has no
        active endpoint subscribed to this type (nothing to do).
        """
        endpoints = self._backend.query(
            "SELECT id, event_types FROM webhook_endpoints"
            " WHERE organization_id = ? AND active = 1",
            (organization_id,),
        )
        targets = [
            row["id"]
            for row in endpoints
            if _endpoint_wants(row["event_types"], event_type)
        ]
        if not targets:
            return None

        existing = self._backend.query(
            "SELECT id FROM webhook_events"
            " WHERE organization_id = ? AND type = ? AND dedupe_key = ?",
            (organization_id, event_type, dedupe_key),
        )
        now = _now()
        statements: list[tuple[str, tuple[Any, ...]]] = []
        if existing:
            event_id = str(existing[0]["id"])
        else:
            event_id = str(uuid.uuid4())
            statements.append(
                (
                    (
                        "INSERT INTO webhook_events"
                        " (id, organization_id, type, dedupe_key, payload,"
                        " occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        event_id,
                        organization_id,
                        event_type,
                        dedupe_key,
                        payload,
                        occurred_at,
                        now,
                    ),
                )
            )

        already = {
            row["webhook_endpoint_id"]
            for row in self._backend.query(
                "SELECT webhook_endpoint_id FROM webhook_deliveries"
                " WHERE webhook_event_id = ?",
                (event_id,),
            )
        }
        for endpoint_id in targets:
            if endpoint_id in already:
                continue
            statements.append(
                (
                    (
                        "INSERT INTO webhook_deliveries (id, organization_id,"
                        " webhook_event_id, webhook_endpoint_id, status, attempts,"
                        " max_attempts, next_attempt_at, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)"
                    ),
                    (
                        str(uuid.uuid4()),
                        organization_id,
                        event_id,
                        endpoint_id,
                        max_attempts,
                        now,
                        now,
                        now,
                    ),
                )
            )
        if statements:
            self._backend.transaction(statements)
        return event_id

    def claim_webhook_deliveries(
        self, limit: int, stale_before: datetime
    ) -> list[dict[str, Any]]:
        """Atomically mark up to ``limit`` due deliveries 'delivering' and return
        them joined with their event and endpoint.
        """
        now = _now()
        due = (
            "SELECT id FROM webhook_deliveries WHERE"
            " (status IN ('pending', 'failed') AND next_attempt_at <= ?)"
            " OR (status = 'delivering' AND locked_at IS NOT NULL AND locked_at < ?)"
            " ORDER BY next_attempt_at LIMIT ?"
        )
        if self.dialect == "postgres":
            due += " FOR UPDATE SKIP LOCKED"
        claimed = self._backend.execute_returning(
            "UPDATE webhook_deliveries SET status = 'delivering', locked_at = ?"
            f" WHERE id IN ({due}) RETURNING id",
            (now, now, stale_before, limit),
        )
        ids = [row["id"] for row in claimed]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        return self._backend.query(
            "SELECT d.id, d.organization_id, d.attempts, d.max_attempts,"
            " d.webhook_event_id, d.webhook_endpoint_id,"
            " e.type AS event_type, e.payload AS event_payload,"
            " w.url AS endpoint_url, w.secret AS endpoint_secret"
            " FROM webhook_deliveries d"
            " JOIN webhook_events e ON e.id = d.webhook_event_id"
            " JOIN webhook_endpoints w ON w.id = d.webhook_endpoint_id"
            f" WHERE d.id IN ({placeholders})",
            tuple(ids),
        )

    def record_webhook_attempt(
        self,
        delivery_id: str,
        *,
        attempt: int,
        status: str,
        status_code: int | None,
        error: str | None,
        duration_ms: int | None,
        response_snippet: str | None,
        next_attempt_at: datetime | None,
    ) -> None:
        now = _now()
        self._backend.transaction(
            [
                (
                    (
                        "INSERT INTO webhook_delivery_attempts (webhook_delivery_id,"
                        " attempt, attempted_at, status_code, error, duration_ms)"
                        " VALUES (?, ?, ?, ?, ?, ?)"
                    ),
                    (delivery_id, attempt, now, status_code, error, duration_ms),
                ),
                (
                    (
                        "UPDATE webhook_deliveries SET status = ?, attempts = ?,"
                        " next_attempt_at = ?, last_attempt_at = ?,"
                        " last_status_code = ?, last_error = ?, response_snippet = ?,"
                        " locked_at = NULL, updated_at = ? WHERE id = ?"
                    ),
                    (
                        status,
                        attempt,
                        next_attempt_at,
                        now,
                        status_code,
                        error,
                        response_snippet,
                        now,
                        delivery_id,
                    ),
                ),
            ]
        )

    def webhook_delivery(
        self, organization_id: str, delivery_id: str
    ) -> dict[str, Any] | None:
        rows = self._backend.query(
            "SELECT d.id, d.organization_id, d.webhook_event_id,"
            " d.webhook_endpoint_id, d.status, d.attempts, d.max_attempts,"
            " d.next_attempt_at, d.last_attempt_at, d.last_status_code,"
            " d.last_error, d.response_snippet, d.created_at,"
            " e.type AS event_type, e.payload AS event_payload"
            " FROM webhook_deliveries d"
            " JOIN webhook_events e ON e.id = d.webhook_event_id"
            " WHERE d.organization_id = ? AND d.id = ?",
            (organization_id, delivery_id),
        )
        if not rows:
            return None
        detail = rows[0]
        detail["history"] = self._backend.query(
            "SELECT attempt, attempted_at, status_code, error, duration_ms"
            " FROM webhook_delivery_attempts WHERE webhook_delivery_id = ?"
            " ORDER BY id",
            (delivery_id,),
        )
        return detail

    def webhook_deliveries_page(
        self,
        organization_id: str,
        endpoint_id: str,
        limit: int,
        status: str | None = None,
        before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT d.id, d.webhook_event_id, d.webhook_endpoint_id, d.status,"
            " d.attempts, d.max_attempts, d.next_attempt_at, d.last_attempt_at,"
            " d.last_status_code, d.last_error, d.created_at, e.type AS event_type"
            " FROM webhook_deliveries d"
            " JOIN webhook_events e ON e.id = d.webhook_event_id"
            " WHERE d.organization_id = ? AND d.webhook_endpoint_id = ?"
        )
        params: list[Any] = [organization_id, endpoint_id]
        if status is not None:
            sql += " AND d.status = ?"
            params.append(status)
        if before_id is not None:
            sql += " AND d.id < ?"
            params.append(before_id)
        sql += " ORDER BY d.id DESC LIMIT ?"
        params.append(limit)
        return self._backend.query(sql, tuple(params))

    def reset_webhook_delivery(self, organization_id: str, delivery_id: str) -> bool:
        now = _now()
        rows = self._backend.execute_returning(
            "UPDATE webhook_deliveries SET status = 'pending', attempts = 0,"
            " next_attempt_at = ?, last_error = NULL, locked_at = NULL,"
            " updated_at = ? WHERE organization_id = ? AND id = ?"
            " AND status IN ('failed', 'dead', 'succeeded') RETURNING id",
            (now, now, organization_id, delivery_id),
        )
        return bool(rows)

    def pending_webhook_delivery_count(self) -> int:
        rows = self._backend.query(
            "SELECT COUNT(*) AS n FROM webhook_deliveries"
            " WHERE status IN ('pending', 'failed')",
            (),
        )
        return int(rows[0]["n"])

    # -- integration connections ---------------------------------------

    _INTEGRATION_COLUMNS = (
        "id, organization_id, provider, status, display_name,"
        " encrypted_credentials, external_account_id, scopes, settings,"
        " token_expires_at, last_error, last_verified_at, created_at, updated_at"
    )

    def integration_connection(
        self, organization_id: str, provider: str
    ) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._INTEGRATION_COLUMNS} FROM integration_connections"
            " WHERE organization_id = ? AND provider = ?",
            (organization_id, provider),
        )
        return rows[0] if rows else None

    def list_integration_connections(
        self, organization_id: str
    ) -> list[dict[str, Any]]:
        return self._backend.query(
            f"SELECT {self._INTEGRATION_COLUMNS} FROM integration_connections"
            " WHERE organization_id = ? ORDER BY provider",
            (organization_id,),
        )

    def active_integration_connections(
        self, organization_id: str
    ) -> list[dict[str, Any]]:
        return self._backend.query(
            f"SELECT {self._INTEGRATION_COLUMNS} FROM integration_connections"
            " WHERE organization_id = ? AND status = 'active' ORDER BY provider",
            (organization_id,),
        )

    def upsert_integration_connection(
        self,
        organization_id: str,
        provider: str,
        *,
        status: str,
        display_name: str | None,
        encrypted_credentials: str,
        external_account_id: str | None,
        scopes: str | None,
        settings: str | None,
        last_error: str | None,
        last_verified_at: datetime | None,
    ) -> str:
        connection_id = str(uuid.uuid4())
        now = _now()
        rows = self._backend.execute_returning(
            "INSERT INTO integration_connections"
            " (id, organization_id, provider, status, display_name,"
            " encrypted_credentials, external_account_id, scopes, settings,"
            " last_error, last_verified_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (organization_id, provider) DO UPDATE SET"
            " status = excluded.status, display_name = excluded.display_name,"
            " encrypted_credentials = excluded.encrypted_credentials,"
            " external_account_id = excluded.external_account_id,"
            " scopes = excluded.scopes, settings = excluded.settings,"
            " last_error = excluded.last_error,"
            " last_verified_at = excluded.last_verified_at,"
            " updated_at = excluded.updated_at"
            " RETURNING id",
            (
                connection_id,
                organization_id,
                provider,
                status,
                display_name,
                encrypted_credentials,
                external_account_id,
                scopes,
                settings,
                last_error,
                last_verified_at,
                now,
                now,
            ),
        )
        return str(rows[0]["id"]) if rows else connection_id

    def set_integration_status(
        self,
        organization_id: str,
        provider: str,
        *,
        status: str,
        last_error: str | None,
        last_verified_at: datetime | None,
    ) -> None:
        self._backend.execute(
            "UPDATE integration_connections SET status = ?, last_error = ?,"
            " last_verified_at = COALESCE(?, last_verified_at), updated_at = ?"
            " WHERE organization_id = ? AND provider = ?",
            (
                status,
                last_error,
                last_verified_at,
                _now(),
                organization_id,
                provider,
            ),
        )

    def delete_integration_connection(
        self, organization_id: str, provider: str
    ) -> bool:
        rows = self._backend.execute_returning(
            "DELETE FROM integration_connections"
            " WHERE organization_id = ? AND provider = ? RETURNING id",
            (organization_id, provider),
        )
        return bool(rows)

    # -- public-API keys ---------------------------------------------

    _API_KEY_COLUMNS = (
        "id, organization_id, name, prefix, scopes, created_by_user_id,"
        " last_used_at, revoked_at, created_at, updated_at"
    )

    def create_api_key(
        self,
        organization_id: str,
        *,
        name: str,
        prefix: str,
        token_hash: str,
        scopes: str,
        created_by_user_id: str | None,
    ) -> str:
        key_id = str(uuid.uuid4())
        now = _now()
        self._backend.execute(
            "INSERT INTO api_keys"
            " (id, organization_id, name, prefix, token_hash, scopes,"
            " created_by_user_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_id,
                organization_id,
                name,
                prefix,
                token_hash,
                scopes,
                created_by_user_id,
                now,
                now,
            ),
        )
        return key_id

    def api_key(self, organization_id: str, key_id: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._API_KEY_COLUMNS} FROM api_keys"
            " WHERE organization_id = ? AND id = ?",
            (organization_id, key_id),
        )
        return rows[0] if rows else None

    def api_key_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._API_KEY_COLUMNS} FROM api_keys WHERE token_hash = ?",
            (token_hash,),
        )
        return rows[0] if rows else None

    def list_api_keys(self, organization_id: str) -> list[dict[str, Any]]:
        return self._backend.query(
            f"SELECT {self._API_KEY_COLUMNS} FROM api_keys"
            " WHERE organization_id = ? ORDER BY created_at DESC, id",
            (organization_id,),
        )

    def revoke_api_key(self, organization_id: str, key_id: str) -> bool:
        rows = self._backend.execute_returning(
            "UPDATE api_keys SET revoked_at = ?, updated_at = ?"
            " WHERE organization_id = ? AND id = ? AND revoked_at IS NULL"
            " RETURNING id",
            (_now(), _now(), organization_id, key_id),
        )
        return bool(rows)

    def rotate_api_key(
        self,
        organization_id: str,
        key_id: str,
        *,
        prefix: str,
        token_hash: str,
    ) -> None:
        self._backend.execute(
            "UPDATE api_keys SET prefix = ?, token_hash = ?, last_used_at = NULL,"
            " updated_at = ? WHERE organization_id = ? AND id = ?"
            " AND revoked_at IS NULL",
            (prefix, token_hash, _now(), organization_id, key_id),
        )

    def touch_api_key(self, key_id: str) -> None:
        self._backend.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (_now(), key_id),
        )

    # -- CRM sync queue --------------------------------------------

    _CRM_JOB_COLUMNS = (
        "id, organization_id, provider, call_id, kind, status, attempts,"
        " max_attempts, next_attempt_at, last_error, result, created_at, updated_at"
    )

    def enqueue_crm_sync_job(
        self,
        organization_id: str,
        provider: str,
        call_id: str,
        kind: str,
        max_attempts: int,
    ) -> str | None:
        """Queue one sync, or return None if an identical job already exists."""
        now = _now()
        rows = self._backend.execute_returning(
            "INSERT INTO crm_sync_jobs"
            " (id, organization_id, provider, call_id, kind, status, attempts,"
            " max_attempts, next_attempt_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)"
            " ON CONFLICT (organization_id, provider, call_id, kind) DO NOTHING"
            " RETURNING id",
            (
                str(uuid.uuid4()),
                organization_id,
                provider,
                call_id,
                kind,
                max_attempts,
                now,
                now,
                now,
            ),
        )
        return str(rows[0]["id"]) if rows else None

    def claim_crm_sync_jobs(
        self, limit: int, stale_before: datetime
    ) -> list[dict[str, Any]]:
        now = _now()
        due = (
            "SELECT id FROM crm_sync_jobs WHERE"
            " (status IN ('pending', 'failed') AND next_attempt_at <= ?)"
            " OR (status = 'syncing' AND locked_at IS NOT NULL AND locked_at < ?)"
            " ORDER BY next_attempt_at LIMIT ?"
        )
        if self.dialect == "postgres":
            due += " FOR UPDATE SKIP LOCKED"
        claimed = self._backend.execute_returning(
            "UPDATE crm_sync_jobs SET status = 'syncing', locked_at = ?"
            f" WHERE id IN ({due}) RETURNING id",
            (now, now, stale_before, limit),
        )
        ids = [row["id"] for row in claimed]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        return self._backend.query(
            f"SELECT {self._CRM_JOB_COLUMNS} FROM crm_sync_jobs"
            f" WHERE id IN ({placeholders})",
            tuple(ids),
        )

    def crm_sync_job(self, organization_id: str, job_id: str) -> dict[str, Any] | None:
        rows = self._backend.query(
            f"SELECT {self._CRM_JOB_COLUMNS} FROM crm_sync_jobs"
            " WHERE organization_id = ? AND id = ?",
            (organization_id, job_id),
        )
        return rows[0] if rows else None

    def update_crm_sync_result(self, job_id: str, result: str) -> None:
        self._backend.execute(
            "UPDATE crm_sync_jobs SET result = ?, updated_at = ? WHERE id = ?",
            (result, _now(), job_id),
        )

    def finish_crm_sync_job(
        self,
        job_id: str,
        *,
        status: str,
        attempts: int,
        next_attempt_at: datetime | None,
        last_error: str | None,
        result: str | None,
    ) -> None:
        self._backend.execute(
            "UPDATE crm_sync_jobs SET status = ?, attempts = ?,"
            " next_attempt_at = ?, last_error = ?, result = COALESCE(?, result),"
            " locked_at = NULL, updated_at = ? WHERE id = ?",
            (
                status,
                attempts,
                next_attempt_at,
                last_error,
                result,
                _now(),
                job_id,
            ),
        )


def _endpoint_wants(event_types: str | None, event_type: str) -> bool:
    if not event_types:
        return True
    try:
        wanted = json.loads(event_types)
    except (TypeError, ValueError):
        return True
    return not isinstance(wanted, list) or event_type in wanted


def transcript_as_text(turns: list[dict[str, str]]) -> str:
    labels = {"caller": "Caller", "agent": "Agent", "tool": "System"}
    return "\n".join(f"{labels.get(t['role'], t['role'])}: {t['text']}" for t in turns)


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
