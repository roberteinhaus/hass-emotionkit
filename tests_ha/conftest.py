"""Fixtures for the tests against the real Home Assistant core.

These tests need Home Assistant installed (see requirements.txt) and are
therefore kept apart from ../tests, which runs anywhere. Run the two suites
as separate commands.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]

# Home Assistant finds custom integrations through the importable package
# `custom_components`. Before it looks, it imports that name with its own
# test config directory first on the path - where the test plugin ships a
# `custom_components` package of its own. Whoever is imported first wins, so
# make sure it is ours: put the integration's root on the path and import
# the package now, at collection time, before any `hass` fixture exists.
if str(INTEGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT))

# ../tests registers an empty shell under the same name to import the
# integration's modules without Home Assistant. Should both suites ever be
# collected in one process, the shell must not be mistaken for the package.
for _name in [
    name
    for name in sys.modules
    if name == "custom_components" or name.startswith("custom_components.")
]:
    if getattr(sys.modules[_name], "__spec__", None) is None:
        del sys.modules[_name]

import custom_components  # noqa: E402

assert any(
    Path(path) == INTEGRATION_ROOT / "custom_components"
    for path in custom_components.__path__
), f"custom_components was imported from {list(custom_components.__path__)}"

from custom_components.emotionkit import coordinator  # noqa: E402

from ha_support import FakeBroker  # noqa: E402


@pytest.fixture
def broker() -> Generator[FakeBroker]:
    """Replace the broker connection.

    `aiomqtt.Client` is the narrowest seam there is: the only object through
    which the integration talks MQTT. No test can reach a real broker.
    """
    fake = FakeBroker()
    with patch.object(coordinator.aiomqtt, "Client", fake.client):
        yield fake


@pytest.fixture
def fast_reconnect() -> Generator[None]:
    """Shorten the pauses between connection attempts from seconds to ms."""
    with (
        patch.object(coordinator, "RECONNECT_DELAYS", (0.01, 0.05)),
        patch.object(coordinator, "AUTH_RETRY_DELAYS", (0.01, 0.05)),
    ):
        yield


@pytest.fixture(autouse=True)
def emotionkit_test_environment(
    broker: FakeBroker,
    fast_reconnect: None,
    enable_custom_integrations: None,
) -> None:
    """Apply to every test: fake broker, short retries, custom integrations.

    The order of the arguments matters. The broker patch is set up before
    `hass` (which `enable_custom_integrations` brings in) and therefore torn
    down after it: while Home Assistant shuts down and unloads the entries,
    `aiomqtt.Client` is still the fake.
    """
    return
