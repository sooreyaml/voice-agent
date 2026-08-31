from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.domains.telephony.provider import (
    TrunkConfiguration,
    TwilioProvisioningService,
)


def test_provider_purchases_in_shared_account_and_reuses_shared_trunk():
    client = MagicMock()
    client.incoming_phone_numbers.list.return_value = []
    number = SimpleNamespace(
        account_sid="AC" + "1" * 32,
        sid="PN" + "2" * 32,
        phone_number="+15550104001",
    )
    client.incoming_phone_numbers.create.return_value = number

    trunks = MagicMock()
    client.trunking.v1.trunks = trunks
    trunks.list.return_value = []
    trunk = SimpleNamespace(
        sid="TK" + "3" * 32,
        friendly_name="OpenAI Realtime Agent",
    )
    trunks.create.return_value = trunk
    trunk_context = MagicMock()
    trunks.return_value = trunk_context
    trunk_context.origination_urls.list.return_value = []
    trunk_context.phone_numbers.list.return_value = []

    factory = MagicMock(return_value=client)
    provider = TwilioProvisioningService(
        "AC" + "1" * 32,
        "secret-token",
        "proj_shared",
        client_factory=factory,
    )
    first = provider.provision_number("+1 (555) 010-4001")

    assert first.account_sid == "AC" + "1" * 32
    assert first.phone_number == "+15550104001"
    factory.assert_called_once_with("AC" + "1" * 32, "secret-token")
    client.incoming_phone_numbers.create.assert_called_once_with(
        phone_number="+15550104001"
    )
    trunks.create.assert_called_once_with(friendly_name="OpenAI Realtime Agent")
    trunk_context.origination_urls.create.assert_called_once_with(
        friendly_name="OpenAI Realtime",
        sip_url="sip:proj_shared@sip.api.openai.com;transport=tls",
        weight=1,
        priority=1,
        enabled=True,
    )
    trunk_context.phone_numbers.create.assert_called_once_with(
        phone_number_sid="PN" + "2" * 32
    )

    client.incoming_phone_numbers.list.return_value = [number]
    trunks.list.return_value = [trunk]
    trunk_context.origination_urls.list.return_value = [
        SimpleNamespace(
            sip_url="sip:proj_shared@sip.api.openai.com;transport=tls",
            enabled=True,
        )
    ]
    trunk_context.phone_numbers.list.return_value = [number]
    second = provider.provision_number("+15550104001")

    assert second == first
    assert client.incoming_phone_numbers.create.call_count == 1
    assert trunks.create.call_count == 1
    assert trunk_context.origination_urls.create.call_count == 1
    assert trunk_context.phone_numbers.create.call_count == 1

    configurations = provider.trunk_configurations()
    assert configurations == [
        TrunkConfiguration(
            sid=trunk.sid,
            friendly_name=trunk.friendly_name,
            origination_urls=((provider.sip_uri, True),),
            phone_numbers=(number.phone_number,),
        )
    ]


def test_provider_search_exposes_address_and_voice_capabilities():
    client = MagicMock()
    local = client.available_phone_numbers.return_value.local
    local.list.return_value = [
        SimpleNamespace(
            phone_number="+442071234567",
            friendly_name="+44 20 7123 4567",
            iso_country="GB",
            locality="London",
            region=None,
            postal_code=None,
            address_requirements="local",
            beta=False,
            capabilities={"voice": True, "sms": False},
        )
    ]
    provider = TwilioProvisioningService(
        "AC" + "1" * 32,
        "secret-token",
        "proj_shared",
        client_factory=MagicMock(return_value=client),
    )

    results = provider.search_available_numbers(
        "GB", "local", contains="+4420", limit=5
    )

    assert results[0]["phone_number"] == "+442071234567"
    assert results[0]["address_requirements"] == "local"
    assert results[0]["capabilities"] == {"voice": True, "sms": False}
    local.list.assert_called_once_with(
        limit=5,
        voice_enabled=True,
        contains="+4420",
    )
