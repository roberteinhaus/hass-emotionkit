"""Device triggers in a real Home Assistant: old automations keep working.

The automations are set up through the real automation integration and are
triggered by messages injected into the broker connection, so the whole
chain is exercised: message loop -> processing -> event bus -> device
trigger -> automation action.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.emotionkit import coordinator
from custom_components.emotionkit.const import ALL_EVENT_TYPES, DOMAIN

from ha_support import (
    CONFIG_TOPIC,
    EVENT_TOPIC,
    MATE,
    OWNER,
    OWNER_CONFIG,
    FakeBroker,
    FakeClient,
    envelope,
    event,
    legacy_entry,
)

# The trigger types of the integration at git HEAD. User automations refer
# to these names.
OLD_TYPES = [
    "bomb_planted",
    "bomb_defused",
    "bomb_exploded",
    "round_live",
    "round_over_t",
    "round_over_ct",
    "freezetime",
    "idle",
]


async def _setup(hass: HomeAssistant, broker: FakeBroker) -> tuple[Any, FakeClient, str]:
    """Set up a legacy entry; return it, its broker session and the device id."""
    entry = legacy_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await broker.wait_ready()
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, client, entry.runtime_data.device_id


def _assert_automations_are_on(hass: HomeAssistant, automation_ids: list[str]) -> None:
    """An automation whose trigger does not validate is not 'on'."""
    registry = er.async_get(hass)
    for automation_id in automation_ids:
        entity_id = registry.async_get_entity_id("automation", "automation", automation_id)
        assert entity_id is not None, f"automation '{automation_id}' was not created"
        state = hass.states.get(entity_id)
        assert state is not None, automation_id
        assert state.state == "on", f"automation '{automation_id}' is {state.state}"


def _count(calls: list[ServiceCall], trigger_type: str) -> int:
    return sum(1 for call in calls if call.data["automation"] == trigger_type)


async def test_all_triggers_are_offered(hass: HomeAssistant, broker: FakeBroker) -> None:
    """The automation editor lists every trigger type for the device."""
    _, _, device_id = await _setup(hass, broker)

    triggers = [
        trigger
        for trigger in await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, device_id
        )
        if trigger["domain"] == DOMAIN
    ]
    assert sorted(trigger["type"] for trigger in triggers) == sorted(ALL_EVENT_TYPES)
    for old_type in OLD_TYPES:
        assert old_type in ALL_EVENT_TYPES
    for trigger in triggers:
        assert trigger["platform"] == "device"
        assert trigger["device_id"] == device_id


async def test_old_format_automations_still_validate_and_fire(
    hass: HomeAssistant, broker: FakeBroker
) -> None:
    """Automations written for the old version fire from the new event data.

    The automations use the YAML of the time (`trigger:` / `platform: device`
    / `action:` / `service:`), exactly as an automations.yaml of today holds
    them.
    """
    _, client, device_id = await _setup(hass, broker)
    calls = async_mock_service(hass, "test", "automation")

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": f"old_{trigger_type}",
                    "alias": f"old {trigger_type}",
                    "trigger": {
                        "platform": "device",
                        "domain": "emotionkit",
                        "device_id": device_id,
                        "type": trigger_type,
                    },
                    "action": {
                        "service": "test.automation",
                        "data": {
                            "automation": trigger_type,
                            "event_type": "{{ trigger.event.data.type }}",
                            "event_device": "{{ trigger.event.data.device_id }}",
                        },
                    },
                }
                for trigger_type in OLD_TYPES
            ]
        },
    )
    await hass.async_block_till_done()
    _assert_automations_are_on(hass, [f"old_{trigger_type}" for trigger_type in OLD_TYPES])

    clock = SimpleNamespace(now=50_000.0)
    with patch.object(coordinator, "time", SimpleNamespace(monotonic=lambda: clock.now)):
        # Joining a running round: the bomb is down, the round is live. The
        # old version derived triggers from this state and fired at once;
        # now nothing may fire, because nothing *happened*.
        await client.deliver(EVENT_TOPIC, envelope(phase="live", bomb="planted", ct=4, t=3))
        await hass.async_block_till_done()
        assert calls == []

        async def play(*events: dict[str, Any], **payload: Any) -> None:
            clock.now += 1
            await client.deliver(EVENT_TOPIC, envelope(*events, at=clock.now - 50_000, **payload))
            await hass.async_block_till_done()

        # The running round ends with the explosion.
        await play(
            event("round_over", round=8, side="T", reason="bomb"),
            event("bomb_exploded", round=8),
            phase="over",
            bomb="exploded",
            win_team="T",
            ct=4,
            t=4,
        )
        # A complete round, won by the CTs through a defuse.
        await play(event("round_freezetime", round=9), phase="freezetime", ct=4, t=4)
        await play(event("round_live", round=9), phase="live", ct=4, t=4)
        await play(event("bomb_planted", round=9), phase="live", bomb="planted", ct=4, t=4)
        await play(
            event("bomb_defused", round=9),
            event("round_over", round=9, side="CT", reason="defuse"),
            phase="over",
            bomb="defused",
            win_team="CT",
            ct=5,
            t=4,
        )
        # Heartbeats of a second player showing the same state change nothing.
        await play(subject=MATE, phase="over", bomb="defused", win_team="CT", ct=5, t=4)
        await play(subject=MATE, phase="over", bomb="defused", win_team="CT", ct=5, t=4)

        assert _count(calls, "bomb_exploded") == 1
        assert _count(calls, "round_over_t") == 1
        assert _count(calls, "freezetime") == 1
        assert _count(calls, "round_live") == 1
        assert _count(calls, "bomb_planted") == 1
        assert _count(calls, "bomb_defused") == 1
        assert _count(calls, "round_over_ct") == 1
        assert _count(calls, "idle") == 0
        assert len(calls) == 7
        for call in calls:
            # The trigger data the automation sees is the event that fired it.
            assert call.data["event_type"] == call.data["automation"]
            assert call.data["event_device"] == device_id

        # No more game data for the idle timeout (30 s): the idle trigger,
        # which never fired in the old version, fires - once.
        clock.now += 31
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
        assert _count(calls, "idle") == 1
        assert len(calls) == 8

        clock.now += 31
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
        await hass.async_block_till_done()
        assert _count(calls, "idle") == 1


async def test_new_style_trigger_fires_exactly_once(
    hass: HomeAssistant, broker: FakeBroker
) -> None:
    """A trigger fires once per event; duplicates and stale messages do not."""
    _, client, device_id = await _setup(hass, broker)
    calls = async_mock_service(hass, "test", "automation")

    watched = ["player_kill", "player_headshot", "bomb_planted", "match_won", "match_left"]
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": f"new_{trigger_type}",
                    "alias": f"new {trigger_type}",
                    "triggers": [
                        {
                            "trigger": "device",
                            "domain": DOMAIN,
                            "device_id": device_id,
                            "type": trigger_type,
                        }
                    ],
                    "actions": [
                        {
                            "action": "test.automation",
                            "data": {
                                "automation": trigger_type,
                                "subject": "{{ trigger.event.data.subject | default('') }}",
                                "value": "{{ trigger.event.data.value | default('') }}",
                                "round": "{{ trigger.event.data.round | default('') }}",
                            },
                        }
                    ],
                }
                for trigger_type in watched
            ]
        },
    )
    await hass.async_block_till_done()
    _assert_automations_are_on(hass, [f"new_{trigger_type}" for trigger_type in watched])

    # The server tells the device who its owner is.
    await client.deliver(CONFIG_TOPIC, OWNER_CONFIG, retain=True)

    kill = envelope(
        event("player_kill", subject=OWNER, side="CT", value=1, round=2),
        envelope_id="kill-1",
        at=0,
    )
    await client.deliver(EVENT_TOPIC, kill)
    await hass.async_block_till_done()
    assert _count(calls, "player_kill") == 1
    assert _count(calls, "player_headshot") == 0
    assert str(calls[0].data["subject"]) == OWNER
    assert str(calls[0].data["value"]) == "1"
    assert str(calls[0].data["round"]) == "2"

    # The very same message again (QoS 1 may deliver twice): nothing.
    await client.deliver(EVENT_TOPIC, kill)
    await hass.async_block_till_done()
    assert _count(calls, "player_kill") == 1

    # A message that arrives two minutes late: the state is updated, but it
    # triggers nothing.
    await client.deliver(
        EVENT_TOPIC,
        envelope(
            event("player_kill", subject=OWNER, side="CT", value=2, round=2),
            event("bomb_planted", round=2),
            bomb="planted",
            at=-120,
        ),
    )
    await hass.async_block_till_done()
    assert _count(calls, "player_kill") == 1
    assert _count(calls, "bomb_planted") == 0

    # Another player's kill is not the owner's.
    await client.deliver(
        EVENT_TOPIC,
        envelope(
            event("player_kill", subject=MATE, side="CT", value=1, headshot=True, round=2),
            subject=MATE,
            bomb="planted",
            at=1,
        ),
    )
    await hass.async_block_till_done()
    assert _count(calls, "player_kill") == 1
    assert _count(calls, "player_headshot") == 0

    # A fresh headshot kill of the owner fires both of its triggers once.
    await client.deliver(
        EVENT_TOPIC,
        envelope(
            event("player_kill", subject=OWNER, side="CT", value=3, headshot=True, round=2),
            bomb="planted",
            at=2,
        ),
    )
    await hass.async_block_till_done()
    assert _count(calls, "player_kill") == 2
    assert _count(calls, "player_headshot") == 1

    # A new round; the plant is reported by the server - and, because two
    # clients disagreed for a moment, a second time. It fires once.
    await client.deliver(
        EVENT_TOPIC, envelope(event("round_live", round=3), phase="live", ct=1, at=3)
    )
    for offset, subject in ((4, OWNER), (5, MATE)):
        await client.deliver(
            EVENT_TOPIC,
            envelope(
                event("bomb_planted", round=3),
                subject=subject,
                phase="live",
                bomb="planted",
                ct=1,
                at=offset,
            ),
        )
    await hass.async_block_till_done()
    assert _count(calls, "bomb_planted") == 1

    # The match ends; the owner plays CT and CT wins.
    await client.deliver(
        EVENT_TOPIC,
        envelope(event("match_over", side="CT"), phase="over", win_team="CT", ct=13, t=4, at=6),
    )
    await hass.async_block_till_done()
    assert _count(calls, "match_won") == 1
    assert _count(calls, "match_left") == 0
