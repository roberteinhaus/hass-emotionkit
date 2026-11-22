"""Runtime of a config entry: the broker connection and everything it feeds.

This is the Home Assistant side of the integration. It moves messages into
the plain-Python EventProcessor (engine.py) and carries the results out:
triggers onto the event bus, state to the entities through the dispatcher.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import time
from typing import Any

import aiomqtt

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util.ssl import client_context

from .api import ApiError, async_fetch_broker_url
from .broker import BrokerAddress, InvalidBrokerURL, parse_broker_url
from .connection import (
    Backoff,
    ConnectionSupervisor,
    FailureKind,
    is_auth_reason_code,
)
from .const import (
    CONF_ALL_PLAYERS,
    CONF_CONTROL_PLANE_URL,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_IDLE_TIMEOUT,
    CONF_MQTT_BROKER,
    CONF_MQTT_BROKER_MANUAL,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_REMOTE_CONFIG,
    DEFAULT_CONTROL_PLANE_URL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_IDLE_TIMEOUT,
    DOMAIN,
    EVENT_EMOTIONKIT,
    EVENT_IDLE,
    MAX_IDLE_TIMEOUT,
    STATE_TIMEOUT,
    TOPIC_CONFIG,
    TOPIC_EVENTS,
    TOPIC_STATUS,
)
from .engine import (
    REMOTE_ACTOR_NAME,
    REMOTE_IDLE_TIMEOUT,
    ActivityWatchdog,
    EventProcessor,
    EventResult,
    clean_remote_baseline,
)
from .protocol import build_offline_payload, build_status_payload
from .state import GameSnapshot

_LOGGER = logging.getLogger(__name__)

type EmotionKitConfigEntry = ConfigEntry[EmotionKitRuntime]

KEEPALIVE = 30
STOP_TIMEOUT = 8.0
# (first, longest) delay in seconds between two connection attempts; the
# delay doubles with every failure in a row.
RECONNECT_DELAYS = (2.0, 300.0)
# The same after the broker refused the credentials. That does not fix
# itself, so there is no point in asking often.
AUTH_RETRY_DELAYS = (30.0, 900.0)
# After this many failed connection attempts in a row the server is asked
# whether the broker has moved (at most once per BROKER_REFRESH_INTERVAL).
BROKER_REFRESH_AFTER_FAILURES = 3
BROKER_REFRESH_INTERVAL = 600.0
# Tracebacks of unexpected message-handling errors logged per connection.
MAX_LOGGED_HANDLER_ERRORS = 5


def _classify_failure(err: BaseException) -> FailureKind:
    """Tell refused credentials from every other connection problem."""
    # aiomqtt raises MqttConnectError (a MqttCodeError that is not exported
    # at package level) when the broker answers the CONNECT with a refusal.
    if (
        isinstance(err, aiomqtt.MqttCodeError)
        and type(err).__name__ == "MqttConnectError"
        and is_auth_reason_code(err.rc)
    ):
        return FailureKind.AUTH
    return FailureKind.TRANSIENT


def idle_timeout_from_options(entry: ConfigEntry) -> int:
    """Return the configured idle timeout in seconds (0 = disabled)."""
    try:
        value = int(entry.options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_IDLE_TIMEOUT
    return min(max(value, 0), MAX_IDLE_TIMEOUT)


def device_name_of(entry: ConfigEntry) -> str:
    """Return the device name (a name set in the web UI wins)."""
    return (
        entry.options.get(CONF_DEVICE_NAME)
        or entry.data.get(CONF_DEVICE_NAME)
        or DEFAULT_DEVICE_NAME
    )


class EmotionKitRuntime:
    """Everything that lives as long as the config entry is loaded."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        address: BrokerAddress,
        device_id: str,
        unique_id: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.address = address
        # Id of the device in Home Assistant's device registry.
        self.device_id = device_id
        # Stable identifier of this installation (survives a re-claim).
        self.unique_id = unique_id

        self._username: str = entry.data[CONF_MQTT_USERNAME]
        self._password: str = entry.data[CONF_MQTT_PASSWORD]
        self._server_device_id: str = entry.data.get(CONF_DEVICE_ID, "")
        self.device_name: str = device_name_of(entry)
        self.idle_timeout: int = idle_timeout_from_options(entry)

        self._status_topic = TOPIC_STATUS.format(self._username)
        self._event_topic = TOPIC_EVENTS.format(self._username)
        self._config_topic = TOPIC_CONFIG.format(self._username)

        self.signal_state = f"{DOMAIN}_{entry.entry_id}_state"
        self.signal_trigger = f"{DOMAIN}_{entry.entry_id}_trigger"

        self.processor = EventProcessor(
            all_players=bool(entry.options.get(CONF_ALL_PLAYERS, False)),
            remote_baseline=clean_remote_baseline(entry.data.get(CONF_REMOTE_CONFIG)),
        )
        self.watchdog = ActivityWatchdog(self.idle_timeout, STATE_TIMEOUT)
        self._supervisor = ConnectionSupervisor(
            connect=self._open_client,
            session=self._serve,
            farewell=self._farewell,
            classify=_classify_failure,
            on_connected=self._handle_connected,
            on_disconnected=self._notify_state,
            on_auth_failed=self._handle_auth_failed,
            on_failure=self._after_failure,
            backoff=Backoff(*RECONNECT_DELAYS),
            auth_backoff=Backoff(*AUTH_RETRY_DELAYS),
            logger=_LOGGER,
            name="the EmotionKit broker",
        )
        self._task: asyncio.Task[None] | None = None
        self._timer: CALLBACK_TYPE | None = None
        # Timer for the bomb warning and the time it is set for.
        self._bomb_timer: CALLBACK_TYPE | None = None
        self._bomb_timer_at: float | None = None
        self._handler_errors = 0
        self._last_broker_refresh: float | None = None

    # --- what the entities read -----------------------------------------

    @property
    def connected(self) -> bool:
        """Return True while the broker connection is up."""
        return self._supervisor.connected

    @property
    def snapshot(self) -> GameSnapshot:
        """Return the current game state."""
        return self.processor.snapshot

    # --- life cycle ------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the connection in the background."""
        if self._task is not None:
            return
        _LOGGER.debug(
            "Starting EmotionKit connection to %s (idle timeout %s s)",
            self.address.describe(),
            self.idle_timeout,
        )
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._supervisor.run(),
            name=f"{DOMAIN}_mqtt_{self.entry.entry_id}",
        )

    async def async_stop(self) -> None:
        """Stop the connection. Never raises."""
        self.watchdog.stop()
        if self._timer is not None:
            self._timer()
            self._timer = None
        if self._bomb_timer is not None:
            self._bomb_timer()
            self._bomb_timer = None
        self._bomb_timer_at = None

        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
            # The task publishes its offline status and disconnects before
            # it ends; give it a moment, but never hang the unload on it.
            await asyncio.wait({task}, timeout=STOP_TIMEOUT)
        if not task.done():
            _LOGGER.debug("EmotionKit connection is still shutting down")
            return
        if not task.cancelled() and (err := task.exception()) is not None:
            _LOGGER.debug("EmotionKit connection ended with %r", err)

    # --- connection ------------------------------------------------------

    @asynccontextmanager
    async def _open_client(self) -> AsyncIterator[aiomqtt.Client]:
        """Connect to the broker the current address points to."""
        address = self.address
        options: dict[str, Any] = {}
        if address.tls:
            # Home Assistant builds this context once at start-up, so the
            # call returns a cached object. The executor is a safeguard: it
            # keeps certificate loading off the event loop should the
            # context ever have to be created here.
            options["tls_context"] = await self.hass.async_add_executor_job(
                client_context
            )
        if address.is_websocket:
            options["transport"] = "websockets"
            options["websocket_path"] = address.websocket_path

        async with aiomqtt.Client(
            hostname=address.host,
            port=address.port,
            username=self._username,
            password=self._password,
            will=aiomqtt.Will(
                topic=self._status_topic,
                payload=self._offline_payload(),
                qos=1,
                retain=True,
            ),
            keepalive=KEEPALIVE,
            **options,
        ) as client:
            yield client

    def _status_payload(self) -> str:
        return build_status_payload(
            device_id=self._server_device_id,
            device_name=self.device_name,
            enabled=self.processor.enabled,
            idle_timeout=self.idle_timeout,
            allowed_subjects=self.processor.allowed_subjects,
        )

    def _offline_payload(self) -> str:
        return build_offline_payload(
            device_id=self._server_device_id, device_name=self.device_name
        )

    async def _publish_status(self, client: aiomqtt.Client) -> None:
        await client.publish(
            self._status_topic, self._status_payload(), qos=1, retain=True
        )

    async def _farewell(self, client: aiomqtt.Client) -> None:
        """Tell the server we are going offline.

        A clean disconnect discards the last will, so without this the
        device would stay "online" on the server after an unload.
        """
        await client.publish(
            self._status_topic, self._offline_payload(), qos=1, retain=True, timeout=2
        )

    async def _serve(self, client: aiomqtt.Client) -> None:
        """Serve one connection until it breaks."""
        self._handler_errors = 0
        await self._publish_status(client)
        await client.subscribe(self._event_topic, qos=1)
        await client.subscribe(self._config_topic, qos=1)

        async for message in client.messages:
            try:
                topic = str(message.topic)
                if topic == self._event_topic:
                    self._apply_event_result(
                        self.processor.handle_event(
                            message.payload, monotonic=time.monotonic()
                        )
                    )
                elif topic == self._config_topic:
                    await self._handle_config(
                        client, message.payload, retained=bool(message.retain)
                    )
            except aiomqtt.MqttError:
                # The connection is in trouble: let the supervisor reconnect.
                raise
            except Exception:  # noqa: BLE001 - one bad message must not end the session
                self._handler_errors += 1
                if self._handler_errors <= MAX_LOGGED_HANDLER_ERRORS:
                    _LOGGER.exception("Unexpected error while handling a message")

    # --- supervisor callbacks --------------------------------------------

    @callback
    def _handle_connected(self) -> None:
        # A connection proves the credentials are fine. If a re-auth was
        # requested during a hiccup of the broker, it is no longer needed.
        try:
            for flow in list(
                self.entry.async_get_active_flows(self.hass, {SOURCE_REAUTH})
            ):
                self.hass.config_entries.flow.async_abort(flow["flow_id"])
        except Exception:  # noqa: BLE001 - purely cosmetic clean-up
            _LOGGER.debug("Could not dismiss a pending re-auth flow", exc_info=True)
        self._notify_state()

    @callback
    def _handle_auth_failed(self) -> None:
        self.entry.async_start_reauth(self.hass)

    async def _after_failure(self, consecutive_failures: int) -> None:
        """Ask the server whether the broker has moved.

        The broker address is handed out by the server at claim time. When
        the connection keeps failing, the address may simply be outdated.
        An address the user entered by hand is never replaced.
        """
        if consecutive_failures < BROKER_REFRESH_AFTER_FAILURES:
            return
        if self.entry.data.get(CONF_MQTT_BROKER_MANUAL):
            return
        now = time.monotonic()
        if (
            self._last_broker_refresh is not None
            and now - self._last_broker_refresh < BROKER_REFRESH_INTERVAL
        ):
            return
        self._last_broker_refresh = now

        control_plane_url = self.entry.data.get(
            CONF_CONTROL_PLANE_URL, DEFAULT_CONTROL_PLANE_URL
        )
        try:
            url = await async_fetch_broker_url(
                async_get_clientsession(self.hass), control_plane_url
            )
            if not url:
                return
            address = parse_broker_url(url)
        except (ApiError, InvalidBrokerURL) as err:
            _LOGGER.debug("Could not refresh the broker address: %s", err)
            return
        if address == self.address:
            return

        _LOGGER.info(
            "The EmotionKit server announces a new broker address, switching to %s",
            address.describe(),
        )
        self.address = address
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_MQTT_BROKER: url}
        )

    # --- messages --------------------------------------------------------

    @callback
    def _apply_event_result(self, result: EventResult) -> None:
        if result.in_match:
            self.watchdog.touch(time.monotonic())
            self._ensure_timer()
        # State first: an automation started by a trigger sees entities
        # that already reflect the event.
        if result.state_changed:
            self._notify_state()
        for trigger in result.triggers:
            self._fire(trigger.type, trigger.data)
        self._sync_bomb_timer()

    async def _handle_config(
        self, client: aiomqtt.Client, payload: object, *, retained: bool
    ) -> None:
        result = self.processor.handle_config(payload, retained=retained)
        if not result.valid:
            return

        if result.enabled_changed:
            _LOGGER.info(
                "EmotionKit device was %s in the web UI",
                "enabled" if self.processor.enabled else "disabled",
            )
            if not self.processor.enabled:
                self.watchdog.stop()

        option_updates = self._apply_remote_settings(result.updates)
        if option_updates or result.baseline_changed:
            self._persist(option_updates)
        if result.state_changed:
            self._notify_state()
        self._sync_bomb_timer()

        # The handshake asks for the full status after every config change.
        await self._publish_status(client)

    @callback
    def _apply_remote_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply settings changed in the web UI; return the options to store."""
        option_updates: dict[str, Any] = {}

        name = updates.get(REMOTE_ACTOR_NAME)
        if isinstance(name, str) and name and name != self.device_name:
            self.device_name = name
            option_updates[CONF_DEVICE_NAME] = name
            dr.async_get(self.hass).async_update_device(self.device_id, name=name)

        timeout = updates.get(REMOTE_IDLE_TIMEOUT)
        if (
            isinstance(timeout, int)
            and not isinstance(timeout, bool)
            and timeout != self.idle_timeout
        ):
            self.idle_timeout = timeout
            option_updates[CONF_IDLE_TIMEOUT] = timeout
            self.watchdog.set_idle_timeout(timeout)
            self._reschedule_timer()

        return option_updates

    @callback
    def _persist(self, option_updates: dict[str, Any]) -> None:
        """Store applied settings and the remembered server-side values."""
        changes: dict[str, Any] = {
            "data": {
                **self.entry.data,
                CONF_REMOTE_CONFIG: dict(self.processor.remote_baseline or {}),
            }
        }
        if option_updates:
            changes["options"] = {**self.entry.options, **option_updates}
        new_name = option_updates.get(CONF_DEVICE_NAME)
        if new_name and self.entry.title in (
            self.entry.data.get(CONF_DEVICE_NAME),
            self.entry.options.get(CONF_DEVICE_NAME),
        ):
            # The title was the device name so far: keep it that way.
            changes["title"] = new_name
        # No update listener is registered, so this does not reload.
        self.hass.config_entries.async_update_entry(self.entry, **changes)

    # --- output ----------------------------------------------------------

    @callback
    def _notify_state(self) -> None:
        async_dispatcher_send(self.hass, self.signal_state)

    @callback
    def _fire(self, trigger_type: str, data: dict[str, Any]) -> None:
        self.hass.bus.async_fire(
            EVENT_EMOTIONKIT,
            {**data, "device_id": self.device_id, "type": trigger_type},
        )
        async_dispatcher_send(self.hass, self.signal_trigger, trigger_type, data)
        _LOGGER.debug("Fired %s: %s", EVENT_EMOTIONKIT, trigger_type)

    # --- idle timer ------------------------------------------------------

    @callback
    def _ensure_timer(self) -> None:
        """Arm the timer for the watchdog's next deadline, if it is not."""
        if self._timer is not None:
            return
        deadline = self.watchdog.next_deadline()
        if deadline is None:
            return
        delay = max(deadline - time.monotonic(), 0.0) + 0.05
        self._timer = async_call_later(self.hass, delay, self._async_timer_fired)

    @callback
    def _reschedule_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None
        self._ensure_timer()

    @callback
    def _async_timer_fired(self, _now: datetime) -> None:
        self._timer = None
        idle, expired = self.watchdog.poll(time.monotonic())
        if expired and self.processor.reset_state():
            self._notify_state()
        self._sync_bomb_timer()
        if idle:
            _LOGGER.debug("No game activity for %s s", self.idle_timeout)
            self._fire(
                EVENT_IDLE,
                {
                    "timeout": self.idle_timeout,
                    "last_event_type": self.processor.last_trigger_type,
                    "last_event_at": self.processor.last_trigger_at,
                },
            )
        self._ensure_timer()

    # --- bomb warning ----------------------------------------------------

    @callback
    def _sync_bomb_timer(self) -> None:
        """Keep the timer in step with what the processor has planned."""
        due = self.processor.bomb_warning_at
        if due == self._bomb_timer_at:
            return
        if self._bomb_timer is not None:
            self._bomb_timer()
            self._bomb_timer = None
        self._bomb_timer_at = due
        if due is not None:
            delay = max(due - time.monotonic(), 0.0)
            self._bomb_timer = async_call_later(
                self.hass, delay, self._async_bomb_timer_fired
            )

    @callback
    def _async_bomb_timer_fired(self, _now: datetime) -> None:
        self._bomb_timer = None
        self._bomb_timer_at = None
        trigger = self.processor.take_bomb_warning(time.monotonic())
        if trigger is not None:
            self._fire(trigger.type, trigger.data)
        # Fired a moment early: the warning is still planned, set it again.
        self._sync_bomb_timer()

    # --- diagnostics -----------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Return runtime details. Contains no credentials and no SteamIDs."""
        processor = self.processor
        supervisor = self._supervisor
        snapshot = processor.snapshot
        roles: dict[str, int] = {}
        for role in processor.allowed_subjects.values():
            roles[role] = roles.get(role, 0) + 1
        return {
            "broker": {
                "address": self.address.describe(),
                "tls": self.address.tls,
                "transport": self.address.transport,
                "manual": bool(self.entry.data.get(CONF_MQTT_BROKER_MANUAL)),
            },
            "connection": {
                "connected": supervisor.connected,
                "connects": supervisor.connects,
                "failures": supervisor.failures,
                "consecutive_failures": supervisor.consecutive_failures,
                "consecutive_auth_failures": supervisor.consecutive_auth_failures,
                "last_failure_kind": (
                    supervisor.last_failure_kind.value
                    if supervisor.last_failure_kind
                    else None
                ),
                "last_error": supervisor.last_error,
                "next_retry_in": supervisor.next_retry_in,
                "handler_errors": self._handler_errors,
            },
            "config": {
                "received": processor.config_received,
                "enabled": processor.enabled,
                "owner_known": processor.owner is not None,
                "allowed_subjects_by_role": roles,
                "react_to_all_players": processor.all_players,
                "idle_timeout": self.idle_timeout,
            },
            "clock": {
                "estimated_offset": processor.clock.offset,
                "last_message_age": processor.clock.last_age,
                "rebaselined": processor.clock.rebaselined,
            },
            "state": {
                "in_match": snapshot.in_match,
                "map": snapshot.map_name,
                "round_phase": snapshot.round_phase,
                "round": snapshot.round_number,
                "bomb_planted": snapshot.bomb_planted,
                "score_ct": snapshot.score_ct,
                "score_t": snapshot.score_t,
                "players_in_match": len(processor.tracker.active_subjects),
                # True: the device's players are opponents, so a match ends
                # without match_won / match_lost.
                "mixed_sides": processor.tracker.mixed_sides,
                "bomb_warning_pending": processor.bomb_warning_at is not None,
            },
            "last_trigger": {
                "type": processor.last_trigger_type,
                "at": processor.last_trigger_at,
            },
            "stats": dict(processor.stats),
        }
