"""Minimal stand-ins for Home Assistant, aiomqtt and voluptuous.

They exist so that the glue code of the integration (coordinator, config
flow, entity platforms, ...) can at least be *executed* in an environment
without Home Assistant: test_glue_smoke.py drives it through its main paths
and catches the mistakes no compiler finds (wrong names, wrong keyword
arguments between the integration's own modules, broken control flow).

What they do NOT do is prove compatibility with Home Assistant: every stub
only models the small part of an API the integration uses, written from
reading the real source. Keyword arguments are deliberately strict (unknown
ones raise TypeError, as the real functions would) so that a typo fails.

install() puts the stubs into sys.modules and returns a function that
removes them again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import dataclasses
import enum
import sys
import types
from typing import Any

SSL_CONTEXT = object()  # what homeassistant.util.ssl.client_context() returns


# ---------------------------------------------------------------------------
# voluptuous
# ---------------------------------------------------------------------------


class Invalid(Exception):
    pass


class _Marker:
    def __init__(self, key: str, default: Any = dataclasses.MISSING, **_: Any) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "key", other) == self.key


class Required(_Marker):
    pass


class Optional(_Marker):
    pass


def _validate(validator: Any, value: Any) -> Any:
    if isinstance(validator, Schema):
        return validator(value)
    if isinstance(validator, dict):
        return Schema(validator)(value)
    if isinstance(validator, type):
        if not isinstance(value, validator):
            raise Invalid(f"expected {validator.__name__}")
        return value
    if callable(validator):
        return validator(value)
    if validator != value:
        raise Invalid(f"expected {validator!r}")
    return value


class Schema:
    def __init__(self, schema: Any, **_: Any) -> None:
        self.schema = schema

    def extend(self, extra: dict[Any, Any]) -> Schema:
        return Schema({**self.schema, **extra})

    def __call__(self, data: Any) -> Any:
        if not isinstance(self.schema, dict):
            return _validate(self.schema, data)
        if not isinstance(data, dict):
            raise Invalid("expected a dictionary")
        result = dict(data)
        for marker, validator in self.schema.items():
            key = getattr(marker, "key", marker)
            if key in data:
                result[key] = _validate(validator, data[key])
            elif isinstance(marker, Required):
                if marker.default is dataclasses.MISSING:
                    raise Invalid(f"required key not provided: {key}")
                result[key] = marker.default
        return result


def All(*validators: Any) -> Callable[[Any], Any]:  # noqa: N802
    def validate(value: Any) -> Any:
        for validator in validators:
            value = _validate(validator, value)
        return value

    return validate


def Coerce(target: type) -> Callable[[Any], Any]:  # noqa: N802
    def validate(value: Any) -> Any:
        try:
            return target(value)
        except (TypeError, ValueError) as err:
            raise Invalid(str(err)) from err

    return validate


def Range(min: Any = None, max: Any = None) -> Callable[[Any], Any]:  # noqa: A002, N802
    def validate(value: Any) -> Any:
        if (min is not None and value < min) or (max is not None and value > max):
            raise Invalid("value out of range")
        return value

    return validate


def In(container: Any) -> Callable[[Any], Any]:  # noqa: N802
    def validate(value: Any) -> Any:
        if value not in container:
            raise Invalid(f"{value!r} is not allowed")
        return value

    return validate


# ---------------------------------------------------------------------------
# aiomqtt
# ---------------------------------------------------------------------------


class MqttError(Exception):
    pass


class MqttCodeError(MqttError):
    def __init__(self, rc: Any, *args: Any) -> None:
        super().__init__(*args)
        self.rc = rc


class MqttConnectError(MqttCodeError):
    """Not exported by the real package either; raised on a refused CONNECT."""


@dataclasses.dataclass(frozen=True)
class Will:
    topic: str
    payload: Any = None
    qos: int = 0
    retain: bool = False
    properties: Any = None


@dataclasses.dataclass
class Message:
    topic: str
    payload: Any
    retain: bool = False
    qos: int = 1


class _Messages:
    def __init__(self, client: Client) -> None:
        self._client = client

    def __aiter__(self) -> _Messages:
        return self

    async def __anext__(self) -> Message:
        item = await self._client.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item


class Client:
    """A scripted broker connection.

    The keyword arguments are those of aiomqtt 2.5.1's Client. Class
    attributes steer what the next connection does; `created` collects all
    instances for inspection.
    """

    created: list[Client] = []
    connect_errors: list[BaseException] = []

    def __init__(
        self,
        hostname: str,
        port: int = 1883,
        *,
        username: str | None = None,
        password: str | None = None,
        logger: Any = None,
        identifier: str | None = None,
        queue_type: Any = None,
        protocol: Any = None,
        will: Will | None = None,
        clean_session: bool | None = None,
        transport: str = "tcp",
        timeout: float | None = None,
        keepalive: int = 60,
        bind_address: str = "",
        bind_port: int = 0,
        clean_start: Any = None,
        max_queued_incoming_messages: int | None = None,
        max_queued_outgoing_messages: int | None = None,
        max_inflight_messages: int | None = None,
        max_concurrent_outgoing_calls: int | None = None,
        properties: Any = None,
        tls_context: Any = None,
        tls_params: Any = None,
        tls_insecure: bool | None = None,
        proxy: Any = None,
        socket_options: Any = None,
        websocket_path: str | None = None,
        websocket_headers: Any = None,
    ) -> None:
        if transport not in ("tcp", "websockets", "unix"):
            raise ValueError(transport)
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.will = will
        self.keepalive = keepalive
        self.transport = transport
        self.tls_context = tls_context
        self.tls_params = tls_params
        self.websocket_path = websocket_path
        self.published: list[dict[str, Any]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.connected = False
        self.disconnected_cleanly = False
        Client.created.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.connect_errors = []

    async def __aenter__(self) -> Client:
        await asyncio.sleep(0)
        if Client.connect_errors:
            raise Client.connect_errors.pop(0)
        self.connected = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.connected and not isinstance(exc, MqttError):
            self.disconnected_cleanly = True
        self.connected = False

    @property
    def messages(self) -> _Messages:
        return _Messages(self)

    async def publish(
        self,
        topic: str,
        payload: Any = None,
        qos: int = 0,
        retain: bool = False,
        properties: Any = None,
        *,
        timeout: float | None = None,
    ) -> None:
        if not self.connected:
            raise MqttError("not connected")
        self.published.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )

    async def subscribe(self, topic: str, qos: int = 0, *, timeout: float | None = None) -> None:
        if not self.connected:
            raise MqttError("not connected")
        self.subscriptions.append((topic, qos))

    # --- test helpers ---

    def deliver(self, topic: str, payload: Any, *, retain: bool = False) -> None:
        self.queue.put_nowait(Message(topic, payload, retain))

    def drop(self) -> None:
        self.queue.put_nowait(MqttError("Disconnected during message iteration"))


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------


def callback(func: Any) -> Any:
    return func


class Platform(enum.StrEnum):
    BINARY_SENSOR = "binary_sensor"
    EVENT = "event"
    SENSOR = "sensor"


class ConfigEntryNotReady(Exception):
    pass


class ConfigEntryError(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntry:
    """Only here to be subscripted in annotations; see FakeEntry."""

    def __class_getitem__(cls, item: Any) -> type:
        return cls


class _Bus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict[str, Any]]] = []
        self.listeners: dict[str, list[Any]] = {}

    def async_fire(self, event_type: str, event_data: dict[str, Any] | None = None) -> None:
        self.fired.append((event_type, dict(event_data or {})))

    def async_listen_once(self, event_type: str, listener: Any) -> Callable[[], None]:
        self.listeners.setdefault(event_type, []).append(listener)
        return lambda: self.listeners[event_type].remove(listener)


class _FlowManager:
    def __init__(self) -> None:
        self.aborted: list[str] = []

    def async_abort(self, flow_id: str) -> None:
        self.aborted.append(flow_id)


class _ConfigEntries:
    def __init__(self, hass: FakeHass) -> None:
        self._hass = hass
        self.flow = _FlowManager()
        self.forwarded: list[Any] = []
        self.unloaded: list[Any] = []
        self.updates: list[dict[str, Any]] = []
        self.reloads: list[str] = []

    async def async_forward_entry_setups(self, entry: Any, platforms: Any) -> None:
        self.forwarded.extend(platforms)

    async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
        self.unloaded.extend(platforms)
        return True

    def async_update_entry(
        self,
        entry: Any,
        *,
        data: Any = None,
        options: Any = None,
        title: str | None = None,
        unique_id: Any = None,
    ) -> bool:
        change = {
            key: value
            for key, value in (("data", data), ("options", options), ("title", title))
            if value is not None
        }
        self.updates.append(change)
        changed = False
        for key, value in change.items():
            if getattr(entry, key) != value:
                setattr(entry, key, dict(value) if isinstance(value, dict) else value)
                changed = True
        return changed

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class FakeHass:
    def __init__(self) -> None:
        self.bus = _Bus()
        self.config_entries = _ConfigEntries(self)
        self.data: dict[str, Any] = {}
        self.timers: list[dict[str, Any]] = []
        self.executor_jobs: list[Any] = []

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        self.executor_jobs.append(func)
        return func(*args)

    def async_create_task(
        self, target: Any, name: str | None = None, eager_start: bool = True
    ) -> asyncio.Task[Any]:
        return asyncio.get_running_loop().create_task(target)


HomeAssistant = FakeHass


class FakeEntry:
    def __init__(
        self,
        *,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
        unique_id: str | None = "dev-1",
        title: str = "Home Assistant",
    ) -> None:
        self.entry_id = "entry-1"
        self.domain = "emotionkit"
        self.unique_id = unique_id
        self.data = dict(data)
        self.options = dict(options or {})
        self.title = title
        self.state = "loaded"
        self.version = 1
        self.minor_version = 1
        self.unload_callbacks: list[Any] = []
        self.active_flows: list[dict[str, Any]] = []
        self.reauth_requests = 0
        self.tasks: list[asyncio.Task[Any]] = []

    def async_create_background_task(
        self, hass: Any, target: Any, name: str, eager_start: bool = True
    ) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(target, name=name)
        self.tasks.append(task)
        return task

    def async_on_unload(self, func: Any) -> None:
        self.unload_callbacks.append(func)

    def async_get_active_flows(self, hass: Any, sources: set[str]) -> Any:
        return (flow for flow in self.active_flows if flow["context"]["source"] in sources)

    def async_start_reauth(self, hass: Any, context: Any = None, data: Any = None) -> None:
        self.reauth_requests += 1


class _Device:
    def __init__(self, device_id: str, **fields: Any) -> None:
        self.id = device_id
        self.identifiers: set[tuple[str, str]] = set()
        self.name: str | None = None
        self.__dict__.update(fields)


class _DeviceRegistry:
    def __init__(self) -> None:
        self.devices: dict[str, _Device] = {}

    def async_get_or_create(
        self,
        *,
        config_entry_id: str,
        identifiers: set[tuple[str, str]] | None = None,
        name: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        entry_type: Any = None,
    ) -> _Device:
        for device in self.devices.values():
            if device.identifiers == identifiers:
                device.name = name
                return device
        device = _Device(
            f"registry-{len(self.devices) + 1}",
            identifiers=set(identifiers or ()),
            name=name,
            manufacturer=manufacturer,
            model=model,
            config_entry_id=config_entry_id,
        )
        self.devices[device.id] = device
        return device

    def async_get(self, device_id: str) -> _Device | None:
        return self.devices.get(device_id)

    def async_update_device(self, device_id: str, *, name: Any = None) -> _Device | None:
        device = self.devices.get(device_id)
        if device is not None and name is not None:
            device.name = name
        return device


def _device_registry(hass: FakeHass) -> _DeviceRegistry:
    return hass.data.setdefault("device_registry", _DeviceRegistry())


class DeviceInfo(dict):  # type: ignore[type-arg]
    pass


def async_dispatcher_connect(hass: FakeHass, signal: str, target: Any) -> Callable[[], None]:
    listeners = hass.data.setdefault("dispatcher", {}).setdefault(signal, [])
    listeners.append(target)
    return lambda: listeners.remove(target)


def async_dispatcher_send(hass: FakeHass, signal: str, *args: Any) -> None:
    for target in list(hass.data.get("dispatcher", {}).get(signal, [])):
        target(*args)


def async_call_later(hass: FakeHass, delay: float, action: Any) -> Callable[[], None]:
    timer = {"delay": delay, "action": action, "cancelled": False}
    hass.timers.append(timer)

    def cancel() -> None:
        timer["cancelled"] = True

    return cancel


class Entity:
    hass: Any = None
    entity_description: Any = None
    _attr_should_poll = True
    _attr_has_entity_name = False

    def async_on_remove(self, func: Any) -> None:
        self.__dict__.setdefault("_on_remove", []).append(func)

    async def async_added_to_hass(self) -> None:
        return None

    def async_write_ha_state(self) -> None:
        self.__dict__["state_writes"] = self.__dict__.get("state_writes", 0) + 1

    def async_entity_id_changed(self, old_entity_id: str) -> None:
        return None


class SensorEntity(Entity):
    pass


class BinarySensorEntity(Entity):
    pass


class EventEntity(Entity):
    _attr_event_types: list[str] = []

    def _trigger_event(
        self, event_type: str, event_attributes: dict[str, Any] | None = None
    ) -> None:
        if event_type not in self._attr_event_types:
            raise ValueError(f"Invalid event type {event_type}")
        self.__dict__.setdefault("triggered", []).append((event_type, event_attributes))


@dataclasses.dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    key: str
    device_class: Any = None
    translation_key: str | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class SensorEntityDescription(_EntityDescription):
    state_class: Any = None
    options: list[str] | None = None
    native_unit_of_measurement: str | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class BinarySensorEntityDescription(_EntityDescription):
    pass


class SensorDeviceClass(enum.StrEnum):
    ENUM = "enum"


class SensorStateClass(enum.StrEnum):
    MEASUREMENT = "measurement"


class BinarySensorDeviceClass(enum.StrEnum):
    RUNNING = "running"


def async_redact_data(data: Any, to_redact: Any) -> Any:
    if isinstance(data, dict):
        return {
            key: "**REDACTED**" if key in to_redact else async_redact_data(value, to_redact)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [async_redact_data(item, to_redact) for item in data]
    return data


class _FlowBase:
    hass: Any = None
    source = "user"
    flow_id = "flow-1"

    def async_show_form(
        self,
        *,
        step_id: str | None = None,
        data_schema: Any = None,
        errors: dict[str, str] | None = None,
        description_placeholders: Any = None,
        last_step: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders,
        }

    def async_show_progress(
        self,
        *,
        step_id: str | None = None,
        progress_action: str,
        description_placeholders: Any = None,
        progress_task: Any = None,
    ) -> dict[str, Any]:
        assert progress_task is not None
        return {
            "type": "progress",
            "step_id": step_id,
            "progress_action": progress_action,
            "description_placeholders": description_placeholders,
            "progress_task": progress_task,
        }

    def async_show_progress_done(self, *, next_step_id: str) -> dict[str, Any]:
        return {"type": "progress_done", "step_id": next_step_id}

    def async_abort(self, *, reason: str, description_placeholders: Any = None) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    def add_suggested_values_to_schema(self, data_schema: Any, suggested_values: Any) -> Any:
        return data_schema

    def async_remove(self) -> None:
        return None


class ConfigFlow(_FlowBase):
    def __init_subclass__(cls, *, domain: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    entry: Any = None
    unique_id: str | None = None

    async def async_set_unique_id(self, unique_id: str | None = None) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def _get_reauth_entry(self) -> Any:
        return self.entry

    def _get_reconfigure_entry(self) -> Any:
        return self.entry

    def async_create_entry(self, *, title: str, data: Any, options: Any = None) -> dict[str, Any]:
        return {"type": "create_entry", "title": title, "data": data}

    def async_update_reload_and_abort(
        self,
        entry: Any,
        *,
        unique_id: Any = None,
        title: Any = None,
        data: Any = None,
        data_updates: Any = None,
        options: Any = None,
        reason: Any = None,
        reload_even_if_entry_is_unchanged: bool = True,
    ) -> dict[str, Any]:
        if data_updates is not None:
            entry.data = {**entry.data, **data_updates}
        return {
            "type": "abort",
            "reason": "reauth_successful" if self.source == "reauth" else "reconfigure_successful",
        }


class OptionsFlowWithReload(_FlowBase):
    config_entry: Any = None

    def async_create_entry(self, *, title: str | None = None, data: Any) -> dict[str, Any]:
        return {"type": "create_entry", "data": data}


class _EventTrigger:
    """homeassistant.components.homeassistant.triggers.event"""

    CONF_PLATFORM = "platform"
    CONF_EVENT_TYPE = "event_type"
    CONF_EVENT_DATA = "event_data"
    attached: list[dict[str, Any]] = []

    TRIGGER_SCHEMA = Schema(
        {
            Required("platform"): "event",
            Required("event_type"): str,
            Optional("event_data"): dict,
        }
    )

    @staticmethod
    async def async_attach_trigger(
        hass: Any, config: Any, action: Any, trigger_info: Any, *, platform_type: str = "event"
    ) -> Callable[[], None]:
        _EventTrigger.attached.append({"config": config, "platform_type": platform_type})
        return lambda: None


def _module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _build_modules() -> dict[str, types.ModuleType]:
    event_trigger = _module(
        "homeassistant.components.homeassistant.triggers.event",
        CONF_PLATFORM=_EventTrigger.CONF_PLATFORM,
        CONF_EVENT_TYPE=_EventTrigger.CONF_EVENT_TYPE,
        CONF_EVENT_DATA=_EventTrigger.CONF_EVENT_DATA,
        TRIGGER_SCHEMA=_EventTrigger.TRIGGER_SCHEMA,
        async_attach_trigger=_EventTrigger.async_attach_trigger,
    )
    device_registry = _module(
        "homeassistant.helpers.device_registry",
        async_get=_device_registry,
        DeviceInfo=DeviceInfo,
    )
    modules = [
        _module(
            "voluptuous",
            Schema=Schema,
            Required=Required,
            Optional=Optional,
            All=All,
            Coerce=Coerce,
            Range=Range,
            In=In,
            Invalid=Invalid,
        ),
        _module(
            "aiomqtt",
            Client=Client,
            Will=Will,
            Message=Message,
            MqttError=MqttError,
            MqttCodeError=MqttCodeError,
        ),
        _module("homeassistant", __path__=[]),
        _module(
            "homeassistant.const",
            Platform=Platform,
            EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
            CONF_DEVICE_ID="device_id",
            CONF_DOMAIN="domain",
            CONF_PLATFORM="platform",
            CONF_TYPE="type",
        ),
        _module(
            "homeassistant.core",
            HomeAssistant=HomeAssistant,
            callback=callback,
            CALLBACK_TYPE=Callable[[], None],
            Event=dict,
        ),
        _module(
            "homeassistant.exceptions",
            ConfigEntryAuthFailed=ConfigEntryAuthFailed,
            ConfigEntryError=ConfigEntryError,
            ConfigEntryNotReady=ConfigEntryNotReady,
        ),
        _module(
            "homeassistant.config_entries",
            SOURCE_REAUTH="reauth",
            ConfigEntry=ConfigEntry,
            ConfigFlow=ConfigFlow,
            ConfigFlowResult=dict,
            OptionsFlowWithReload=OptionsFlowWithReload,
        ),
        _module("homeassistant.helpers", __path__=[], device_registry=device_registry),
        device_registry,
        _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: hass.data["session"],
        ),
        _module(
            "homeassistant.helpers.dispatcher",
            async_dispatcher_connect=async_dispatcher_connect,
            async_dispatcher_send=async_dispatcher_send,
        ),
        _module("homeassistant.helpers.event", async_call_later=async_call_later),
        _module("homeassistant.helpers.entity", Entity=Entity),
        _module(
            "homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=Callable
        ),
        _module("homeassistant.helpers.typing", StateType=Any, ConfigType=dict),
        _module("homeassistant.helpers.trigger", TriggerActionType=Any, TriggerInfo=dict),
        _module("homeassistant.util", __path__=[]),
        _module("homeassistant.util.ssl", client_context=lambda: SSL_CONTEXT),
        _module("homeassistant.components", __path__=[]),
        _module(
            "homeassistant.components.sensor",
            SensorDeviceClass=SensorDeviceClass,
            SensorEntity=SensorEntity,
            SensorEntityDescription=SensorEntityDescription,
            SensorStateClass=SensorStateClass,
        ),
        _module(
            "homeassistant.components.binary_sensor",
            BinarySensorDeviceClass=BinarySensorDeviceClass,
            BinarySensorEntity=BinarySensorEntity,
            BinarySensorEntityDescription=BinarySensorEntityDescription,
        ),
        _module("homeassistant.components.event", EventEntity=EventEntity),
        _module("homeassistant.components.diagnostics", async_redact_data=async_redact_data),
        _module(
            "homeassistant.components.device_automation",
            DEVICE_TRIGGER_BASE_SCHEMA=Schema(
                {
                    Required("platform"): "device",
                    Required("domain"): str,
                    Required("device_id"): str,
                }
            ),
        ),
        _module("homeassistant.components.homeassistant", __path__=[]),
        _module(
            "homeassistant.components.homeassistant.triggers",
            __path__=[],
            event=event_trigger,
        ),
        event_trigger,
    ]
    return {module.__name__: module for module in modules}


def real_home_assistant_available() -> bool:
    """Return True if the real package can be imported (stubs not wanted)."""
    if "homeassistant" in sys.modules:
        return not getattr(sys.modules["homeassistant"], "_emotionkit_stub", False)
    import importlib.util

    return importlib.util.find_spec("homeassistant") is not None


def install() -> Callable[[], None]:
    """Put the stubs into sys.modules; return a function that undoes it."""
    modules = _build_modules()
    modules["homeassistant"]._emotionkit_stub = True  # noqa: SLF001
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    Client.reset()
    _EventTrigger.attached = []

    def uninstall() -> None:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    return uninstall
