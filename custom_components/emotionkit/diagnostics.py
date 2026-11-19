"""Diagnostics for the EmotionKit integration.

Credentials and identifiers are redacted; the runtime part is assembled
without SteamIDs in the first place.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_MQTT_PASSWORD, CONF_MQTT_USERNAME
from .coordinator import EmotionKitConfigEntry

TO_REDACT = {CONF_MQTT_PASSWORD, CONF_MQTT_USERNAME, CONF_DEVICE_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EmotionKitConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # Diagnostics can be requested for an entry that failed to set up.
    runtime = getattr(entry, "runtime_data", None)
    return {
        "entry": {
            "title": entry.title,
            "state": str(entry.state),
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "runtime": runtime.diagnostics() if runtime is not None else None,
    }
