"""Binary sensors of the EmotionKit integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EmotionKitConfigEntry, EmotionKitRuntime
from .entity import EmotionKitEntity
from .state import GameSnapshot

# The entities are pushed to; nothing is fetched.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EmotionKitBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an EmotionKit binary sensor."""

    is_on_fn: Callable[[GameSnapshot], bool]


# The keys are listed in const.BINARY_SENSOR_KEYS (checked against the
# translations by the unit tests). Outside a match both are off.
BINARY_SENSORS: tuple[EmotionKitBinarySensorDescription, ...] = (
    EmotionKitBinarySensorDescription(
        key="bomb_planted",
        is_on_fn=lambda snapshot: snapshot.bomb_planted,
    ),
    EmotionKitBinarySensorDescription(
        key="in_match",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on_fn=lambda snapshot: snapshot.in_match,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EmotionKitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        EmotionKitBinarySensor(runtime, description) for description in BINARY_SENSORS
    )


class EmotionKitBinarySensor(EmotionKitEntity, BinarySensorEntity):
    """A yes/no fact about the running match."""

    entity_description: EmotionKitBinarySensorDescription

    def __init__(
        self,
        runtime: EmotionKitRuntime,
        description: EmotionKitBinarySensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return the state; off outside a match."""
        return self.entity_description.is_on_fn(self._runtime.snapshot)
