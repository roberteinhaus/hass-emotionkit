"""Sensors of the EmotionKit integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import ROUND_PHASES
from .coordinator import EmotionKitConfigEntry, EmotionKitRuntime
from .entity import EmotionKitEntity
from .state import GameSnapshot

# The entities are pushed to; nothing is fetched.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EmotionKitSensorDescription(SensorEntityDescription):
    """Describes an EmotionKit sensor."""

    value_fn: Callable[[GameSnapshot], StateType]


# The keys are listed in const.SENSOR_KEYS (checked against the translations
# by the unit tests). Outside a match every value is None, i.e. "unknown".
SENSORS: tuple[EmotionKitSensorDescription, ...] = (
    EmotionKitSensorDescription(
        key="round_phase",
        device_class=SensorDeviceClass.ENUM,
        options=list(ROUND_PHASES),
        value_fn=lambda snapshot: snapshot.round_phase,
    ),
    EmotionKitSensorDescription(
        key="map",
        value_fn=lambda snapshot: snapshot.map_name,
    ),
    EmotionKitSensorDescription(
        key="score_ct",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.score_ct,
    ),
    EmotionKitSensorDescription(
        key="score_t",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.score_t,
    ),
    EmotionKitSensorDescription(
        key="round",
        value_fn=lambda snapshot: snapshot.round_number,
    ),
    # The owner's own values (not those of a spectated teammate).
    EmotionKitSensorDescription(
        key="health",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.health,
    ),
    EmotionKitSensorDescription(
        key="armor",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.armor,
    ),
    EmotionKitSensorDescription(
        key="money",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.money,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EmotionKitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    runtime = entry.runtime_data
    async_add_entities(EmotionKitSensor(runtime, description) for description in SENSORS)


class EmotionKitSensor(EmotionKitEntity, SensorEntity):
    """A value of the running match."""

    entity_description: EmotionKitSensorDescription

    def __init__(
        self, runtime: EmotionKitRuntime, description: EmotionKitSensorDescription
    ) -> None:
        """Initialize."""
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        """Return the value, None (unknown) outside a match."""
        return self.entity_description.value_fn(self._runtime.snapshot)
