from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.domains.businesses.normalization import normalize_e164

from .constants import NANP_COUNTRIES, SHARED_TRUNK_NAME

T = TypeVar("T")


class TelephonyProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code[:64]
        self.message = message[:1000]
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class ProvisioningResult:
    account_sid: str
    phone_number_sid: str
    trunk_sid: str
    phone_number: str


@dataclass(frozen=True)
class TrunkConfiguration:
    sid: str
    friendly_name: str
    origination_urls: tuple[tuple[str, bool], ...]
    phone_numbers: tuple[str, ...]


class TwilioProvisioningService:
    """Shared-account Twilio operations used by both the API and CLI.

    All customer numbers live in the platform's configured Twilio account and
    attach to one shared SIP trunk. The call runtime selects the tenant from the
    destination E.164 number, so no customer Twilio account is required.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        openai_project_id: str,
        *,
        client_factory: Callable[..., Client] = Client,
    ) -> None:
        self.account_sid = account_sid.strip()
        self._auth_token = auth_token.strip()
        self.openai_project_id = openai_project_id.strip()
        self._client_factory = client_factory
        self._client: Client | None = None

    def _twilio(self) -> Client:
        if not self.account_sid or not self._auth_token:
            raise TelephonyProviderError(
                "provider_not_configured",
                "Twilio provisioning is not configured on this deployment.",
            )
        if self._client is None:
            self._client = self._client_factory(self.account_sid, self._auth_token)
        return self._client

    @property
    def sip_uri(self) -> str:
        if not self.openai_project_id.startswith("proj_"):
            raise TelephonyProviderError(
                "provider_not_configured",
                "OPENAI_PROJECT_ID must be configured before provisioning a trunk.",
            )
        return f"sip:{self.openai_project_id}@sip.api.openai.com;transport=tls"

    @staticmethod
    def _invoke(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except TwilioRestException as exc:
            status = int(exc.status) if exc.status is not None else None
            code = str(exc.code or status or "request_failed")
            message = str(exc.msg or "Twilio rejected the request.")
            raise TelephonyProviderError(
                f"twilio_{code}",
                message,
                status=status,
                retryable=status == 429 or bool(status and status >= 500),
            ) from exc

    def regulatory_requirements(
        self, country_code: str, number_type: str, end_user_type: str
    ) -> list[dict[str, Any]]:
        client = self._twilio()
        rows = self._invoke(
            lambda: client.numbers.v2.regulatory_compliance.regulations.list(
                iso_country=country_code.lower(),
                number_type=number_type,
                end_user_type=end_user_type,
                include_constraints=True,
                limit=20,
            )
        )
        return [
            {
                "sid": str(row.sid),
                "friendly_name": str(row.friendly_name or ""),
                "country_code": str(row.iso_country or country_code).upper(),
                "number_type": str(row.number_type or number_type),
                "end_user_type": str(row.end_user_type or end_user_type),
                "requirements": row.requirements or {},
            }
            for row in rows
        ]

    def search_available_numbers(
        self,
        country_code: str,
        number_type: str,
        *,
        area_code: int | None = None,
        contains: str | None = None,
        exclude_address_required: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        country = country_code.upper()
        if area_code is not None and country not in NANP_COUNTRIES:
            raise ValueError("area_code is only supported for US and CA numbers")
        kwargs: dict[str, Any] = {
            "limit": limit,
            "voice_enabled": True,
        }
        if area_code is not None:
            kwargs["area_code"] = area_code
        if contains:
            kwargs["contains"] = contains
        if exclude_address_required:
            kwargs["exclude_all_address_required"] = True
        context = getattr(self._twilio().available_phone_numbers(country), number_type)
        numbers = self._invoke(lambda: context.list(**kwargs))
        return [
            {
                "phone_number": normalize_e164(str(number.phone_number)),
                "friendly_name": str(number.friendly_name or number.phone_number),
                "country_code": str(number.iso_country or country).upper(),
                "locality": str(number.locality) if number.locality else None,
                "region": str(number.region) if number.region else None,
                "postal_code": (
                    str(number.postal_code) if number.postal_code else None
                ),
                "address_requirements": str(number.address_requirements or "none"),
                "beta": bool(number.beta),
                "capabilities": (
                    number.capabilities
                    if isinstance(number.capabilities, dict)
                    else {"voice": True}
                ),
            }
            for number in numbers
        ]

    def provision_number(
        self,
        phone_number: str,
        *,
        address_sid: str | None = None,
        bundle_sid: str | None = None,
        identity_sid: str | None = None,
        trunk_domain: str | None = None,
    ) -> ProvisioningResult:
        client = self._twilio()
        target_number = normalize_e164(phone_number)
        owned = self._invoke(lambda: client.incoming_phone_numbers.list(limit=1000))
        number = next(
            (
                item
                for item in owned
                if normalize_e164(str(item.phone_number)) == target_number
            ),
            None,
        )
        if number is None:
            purchase: dict[str, str] = {"phone_number": target_number}
            if address_sid:
                purchase["address_sid"] = address_sid
            if bundle_sid:
                purchase["bundle_sid"] = bundle_sid
            if identity_sid:
                purchase["identity_sid"] = identity_sid
            number = self._invoke(
                lambda: client.incoming_phone_numbers.create(**purchase)
            )

        trunks = self._invoke(lambda: client.trunking.v1.trunks.list(limit=50))
        trunk = next(
            (item for item in trunks if item.friendly_name == SHARED_TRUNK_NAME),
            None,
        )
        if trunk is None:
            create: dict[str, str] = {"friendly_name": SHARED_TRUNK_NAME}
            if trunk_domain:
                create["domain_name"] = trunk_domain
            trunk = self._invoke(lambda: client.trunking.v1.trunks.create(**create))

        trunk_context = client.trunking.v1.trunks(trunk.sid)
        urls = self._invoke(lambda: trunk_context.origination_urls.list(limit=20))
        if not any(url.sip_url == self.sip_uri and bool(url.enabled) for url in urls):
            self._invoke(
                lambda: trunk_context.origination_urls.create(
                    friendly_name="OpenAI Realtime",
                    sip_url=self.sip_uri,
                    weight=1,
                    priority=1,
                    enabled=True,
                )
            )

        attached = self._invoke(lambda: trunk_context.phone_numbers.list(limit=1000))
        if not any(item.sid == number.sid for item in attached):
            self._invoke(
                lambda: trunk_context.phone_numbers.create(phone_number_sid=number.sid)
            )

        return ProvisioningResult(
            account_sid=str(number.account_sid or self.account_sid),
            phone_number_sid=str(number.sid),
            trunk_sid=str(trunk.sid),
            phone_number=target_number,
        )

    def trunk_configurations(self) -> list[TrunkConfiguration]:
        """Return a secret-free view of the account's SIP trunk routing."""
        client = self._twilio()
        trunks = self._invoke(lambda: client.trunking.v1.trunks.list(limit=50))
        configurations: list[TrunkConfiguration] = []
        for trunk in trunks:
            context = client.trunking.v1.trunks(trunk.sid)
            urls = self._invoke(
                lambda context=context: context.origination_urls.list(limit=20)
            )
            numbers = self._invoke(
                lambda context=context: context.phone_numbers.list(limit=1000)
            )
            configurations.append(
                TrunkConfiguration(
                    sid=str(trunk.sid),
                    friendly_name=str(trunk.friendly_name or ""),
                    origination_urls=tuple(
                        (str(url.sip_url), bool(url.enabled)) for url in urls
                    ),
                    phone_numbers=tuple(str(number.phone_number) for number in numbers),
                )
            )
        return configurations
