"""Event entity of the EmotionKit integration.

One entity that mirrors the device triggers, for those who prefer entity
based automations ("when the game event entity reports bomb_planted").
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ALL_EVENT_TYPES, EVENT_ENTITY_KEY
from .coordinator import EmotionKitConfigEntry, EmotionKitRuntime
from .entity import EmotionKitEntity

# The entity is pushed to; nothing is fetched.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EmotionKitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the event entity."""
    async_add_entities([EmotionKitGameEvent(entry.runtime_data)])


class EmotionKitGameEvent(EmotionKitEntity, EventEntity):
    """The most recent game event."""

    _attr_event_types = list(ALL_EVENT_TYPES)

    def __init__(self, runtime: EmotionKitRuntime) -> None:
        """Initialize."""
        super().__init__(runtime, EVENT_ENTITY_KEY)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the triggers."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._runtime.signal_trigger, self._async_handle_trigger
            )
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes (Home Assistant 2026.11+)."""
        super().async_entity_id_changed(old_entity_id)

    @callback
    def _async_handle_trigger(self, trigger_type: str, data: dict[str, Any]) -> None:
        if trigger_type not in self._attr_event_types:
            return
        self._trigger_event(trigger_type, dict(data))
        self.async_write_ha_state()
