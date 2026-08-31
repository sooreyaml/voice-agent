"""Provider-agnostic contracts for the integration connectors.

Implementations are synchronous (called from the async runtime via
``run_in_threadpool`` under a hard timeout, like the Twilio and Stripe services).
Every failure path raises a :class:`ProviderError` so callers can degrade to
capturing a follow-up instead of stalling the caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar


class ProviderError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code[:64]
        self.message = message[:500]
        self.retryable = retryable


class CalendarProviderError(ProviderError):
    """Raised by :class:`CalendarProvider` implementations."""


class CrmProviderError(ProviderError):
    """Raised by :class:`CrmProvider` implementations."""


# -- calendar --------------------------------------------------------


@dataclass(frozen=True)
class TimeSlot:
    """A bookable window. Both ends are timezone-aware."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class Booking:
    id: str
    start: datetime
    end: datetime
    location: str | None = None
    reschedule_url: str | None = None
    cancel_url: str | None = None


class CalendarProvider(ABC):
    provider: ClassVar[str]

    @abstractmethod
    def verify(self) -> dict[str, Any]:
        """Live credential check. Returns at least ``{"external_account_id": ...}``.

        Raises :class:`CalendarProviderError` if the provider rejects the call.
        """

    @abstractmethod
    def available_slots(
        self, *, start: datetime, end: datetime, duration_minutes: int
    ) -> list[TimeSlot]:
        """Free windows between ``start`` and ``end`` (both tz-aware)."""

    @abstractmethod
    def create_booking(
        self,
        *,
        start: datetime,
        duration_minutes: int,
        name: str,
        phone: str | None = None,
        email: str | None = None,
        notes: str | None = None,
        timezone: str = "UTC",
    ) -> Booking:
        """Book ``start`` for the given attendee."""

    @abstractmethod
    def cancel_booking(self, booking_id: str, *, reason: str | None = None) -> None:
        """Cancel an existing booking."""

    @abstractmethod
    def reschedule_booking(
        self, booking_id: str, *, start: datetime
    ) -> Booking:
        """Move an existing booking to ``start``."""


# -- CRM -----------------------------------------------------------


@dataclass(frozen=True)
class CrmContact:
    id: str
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None


class CrmProvider(ABC):
    provider: ClassVar[str]

    @abstractmethod
    def verify(self) -> dict[str, Any]:
        """Live credential check. Returns at least ``{"external_account_id": ...}``.

        Raises :class:`CrmProviderError` if the provider rejects the call.
        """

    @abstractmethod
    def find_contact(
        self, *, phone: str | None = None, email: str | None = None
    ) -> CrmContact | None:
        """Look up one contact by phone or email, or None."""

    @abstractmethod
    def upsert_contact(
        self,
        *,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
    ) -> CrmContact:
        """Return the existing contact for this phone/email, or create one."""

    @abstractmethod
    def add_note(self, *, contact_id: str, body: str) -> str:
        """Attach a note to a contact. Returns the note id."""

    @abstractmethod
    def create_task(
        self,
        *,
        contact_id: str | None,
        title: str,
        body: str | None = None,
        due_at: datetime | None = None,
    ) -> str:
        """Create a follow-up task. Returns the task id."""
