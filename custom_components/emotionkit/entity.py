"""Base class of the EmotionKit entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import EmotionKitRuntime


class EmotionKitEntity(Entity):
    """An entity fed by the runtime through the dispatcher (no polling)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: EmotionKitRuntime, key: str) -> None:
        """Initialize."""
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.unique_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.unique_id)}
        )

    @property
    def available(self) -> bool:
        """Return True while the broker connection is up."""
        return self._runtime.connected

    async def async_added_to_hass(self) -> None:
        """Subscribe to state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._runtime.signal_state, self._async_handle_state
            )
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes (Home Assistant 2026.11+).

        Nothing here is keyed on the entity_id, so there is nothing to
        update. Older versions never call this.
        """
        super().async_entity_id_changed(old_entity_id)  # type: ignore[misc]

    @callback
    def _async_handle_state(self) -> None:
        self.async_write_ha_state()
