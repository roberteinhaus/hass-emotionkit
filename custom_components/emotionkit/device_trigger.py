"""Device triggers for EmotionKit — CS2 game events as automation triggers."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import ALL_EVENT_TYPES, DOMAIN, EVENT_EMOTIONKIT

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(ALL_EVENT_TYPES)}
)

# Human-readable labels shown in the Automation editor.
TRIGGER_TYPE_LABELS: dict[str, str] = {
    "bomb_planted": "Bomb planted",
    "bomb_defused": "Bomb defused",
    "bomb_exploded": "Bomb exploded",
    "round_live": "Round started (live)",
    "round_over_t": "Round won by Terrorists",
    "round_over_ct": "Round won by Counter-Terrorists",
    "freezetime": "Freeze time",
}


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Return a list of triggers for an EmotionKit device."""
    device_reg = dr.async_get(hass)
    device = device_reg.async_get(device_id)
    if device is None:
        return []

    # Verify this device belongs to the emotionkit domain.
    if not any(ident[0] == DOMAIN for ident in device.identifiers):
        return []

    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: event_type,
        }
        for event_type in ALL_EVENT_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger that fires on a specific EmotionKit event type."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_EMOTIONKIT,
            event_trigger.CONF_EVENT_DATA: {
                "device_id": config[CONF_DEVICE_ID],
                "type": config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """Return additional trigger capabilities (none needed)."""
    return {}
