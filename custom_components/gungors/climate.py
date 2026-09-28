"""Sync thermostat climate platform (gungors).

Inherits the SmartThermostat (PID) climate entity from ScratMan/HASmartThermostat
and keeps it in two-way sync with a physical thermostat (TRV) climate entity.
ScratMan's integration must be installed in custom_components/smart_thermostat.

This module is the only place that imports smart_thermostat, so a problem there
cannot keep the other gungors platforms (e.g. cover) from loading.

Previously shipped as the separate `sync_thermostat` integration.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, EventStateChangedData, State, callback
from homeassistant.helpers import entity_platform
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoredExtraData

# The original integration must be installed next to this one.
from custom_components.smart_thermostat import const as st_const
from custom_components.smart_thermostat.climate import (
    PLATFORM_SCHEMA as ST_PLATFORM_SCHEMA,
    SmartThermostat,
)

from . import PLATFORMS
from .const import (
    ATTR_PHYSICAL_THERMOSTAT,
    ATTR_PRE_OFF_TARGET_TEMP,
    CONF_COOLDOWN_TIME,
    CONF_ENTITY,
    CONF_INPUT_DEVICE,
    CONF_VALVE_CLOSING,
    CONF_VALVE_MIN,
    CONF_VALVE_OPENING,
    DEFAULT_COOLDOWN_TIME,
    DEFAULT_VALVE_MIN,
    DOMAIN,
    PARAM_HEATER_VALVES,
    PUSH_ECHO_TIMEOUT,
    TEMP_TOLERANCE,
)

_LOGGER = logging.getLogger(__name__)

CLIMATE_DOMAIN = "climate"
SERVICE_SET_TEMPERATURE = "set_temperature"
SERVICE_SET_HVAC_MODE = "set_hvac_mode"
ATTR_HVAC_MODE = "hvac_mode"
ATTR_MIN_TEMP = "min_temp"
ATTR_TARGET_TEMP_STEP = "target_temp_step"
ATTR_HVAC_MODES = "hvac_modes"

# Key in the extra restore data holding the last state written while available.
# An unavailable state is stored without its attributes (setpoint, pid_i, ...), so
# restoring from it after a restart would lose them.
LAST_AVAILABLE_STATE = "last_available_state"

# When the physical thermostat is switched on from OFF, wait (seconds) for it to
# report the new mode before writing the setpoint. Some devices (e.g. EMS-ESP hc1)
# store a setpoint received while still OFF as their "off temperature".
PHYSICAL_MODE_WAIT = 10

# `heater` as a dict: instead of writing the PID output to a heater entity
# (e.g. an input_number), it is scaled onto the TRV valve opening/closing degrees.
HEATER_VALVES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_VALVE_OPENING): cv.entity_ids,
        vol.Required(CONF_VALVE_CLOSING): cv.entity_ids,
        vol.Optional(CONF_VALVE_MIN, default=DEFAULT_VALVE_MIN): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
    }
)

# `input_device`: the physical thermostat (climate entity) kept in sync with this one.
# `cooldown_time` is the settle time after its last change before it is applied.
INPUT_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY): cv.entity_id,
        vol.Optional(CONF_COOLDOWN_TIME, default=DEFAULT_COOLDOWN_TIME): vol.All(
            cv.time_period, cv.positive_timedelta
        ),
    }
)

# Same schema as the original, plus `input_device` and the alternative dict form
# of `heater`.
PLATFORM_SCHEMA = ST_PLATFORM_SCHEMA.extend(
    {
        vol.Required(st_const.CONF_HEATER): vol.Any(HEATER_VALVES_SCHEMA, cv.entity_ids),
        vol.Required(CONF_INPUT_DEVICE): INPUT_DEVICE_SCHEMA,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the sync thermostat platform."""
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    platform = entity_platform.current_platform.get()
    assert platform

    c = st_const

    heater = config.get(c.CONF_HEATER)
    pwm = config.get(c.CONF_PWM)
    heater_valves = None
    if isinstance(heater, dict):
        # Valve mapping mode: the value is applied directly, so no PWM.
        heater_valves = heater
        heater_entities = list(
            dict.fromkeys([*heater[CONF_VALVE_OPENING], *heater[CONF_VALVE_CLOSING]])
        )
        if pwm is not None and pwm.total_seconds():
            _LOGGER.warning(
                "%s: pwm is ignored (forced to 0) when heater is a valve mapping",
                config.get(CONF_NAME),
            )
        pwm = timedelta(0)
    else:
        heater_entities = heater

    parameters = {
        "name": config.get(CONF_NAME),
        "unique_id": config.get(CONF_UNIQUE_ID),
        "heater_entity_id": heater_entities,
        "cooler_entity_id": config.get(c.CONF_COOLER),
        "invert_heater": config.get(c.CONF_INVERT_HEATER),
        "sensor_entity_id": config.get(c.CONF_SENSOR),
        "ext_sensor_entity_id": config.get(c.CONF_OUTDOOR_SENSOR),
        "min_temp": config.get(c.CONF_MIN_TEMP),
        "max_temp": config.get(c.CONF_MAX_TEMP),
        "target_temp": config.get(c.CONF_TARGET_TEMP),
        "hot_tolerance": config.get(c.CONF_HOT_TOLERANCE),
        "cold_tolerance": config.get(c.CONF_COLD_TOLERANCE),
        "ac_mode": config.get(c.CONF_AC_MODE),
        "force_off_state": config.get(c.CONF_FORCE_OFF_STATE),
        "min_cycle_duration": config.get(c.CONF_MIN_CYCLE_DURATION),
        "min_off_cycle_duration": config.get(c.CONF_MIN_OFF_CYCLE_DURATION),
        "min_cycle_duration_pid_off": config.get(c.CONF_MIN_CYCLE_DURATION_PID_OFF),
        "min_off_cycle_duration_pid_off": config.get(
            c.CONF_MIN_OFF_CYCLE_DURATION_PID_OFF
        ),
        "keep_alive": config.get(c.CONF_KEEP_ALIVE),
        "sampling_period": config.get(c.CONF_SAMPLING_PERIOD),
        "sensor_stall": config.get(c.CONF_SENSOR_STALL),
        "output_safety": config.get(c.CONF_OUTPUT_SAFETY),
        "initial_hvac_mode": config.get(c.CONF_INITIAL_HVAC_MODE),
        "preset_sync_mode": config.get(c.CONF_PRESET_SYNC_MODE),
        "away_temp": config.get(c.CONF_AWAY_TEMP),
        "eco_temp": config.get(c.CONF_ECO_TEMP),
        "boost_temp": config.get(c.CONF_BOOST_TEMP),
        "comfort_temp": config.get(c.CONF_COMFORT_TEMP),
        "home_temp": config.get(c.CONF_HOME_TEMP),
        "sleep_temp": config.get(c.CONF_SLEEP_TEMP),
        "activity_temp": config.get(c.CONF_ACTIVITY_TEMP),
        "precision": config.get(c.CONF_PRECISION),
        "target_temp_step": config.get(c.CONF_TARGET_TEMP_STEP),
        "unit": hass.config.units.temperature_unit,
        "output_precision": config.get(c.CONF_OUTPUT_PRECISION),
        "output_min": config.get(c.CONF_OUTPUT_MIN),
        "output_max": config.get(c.CONF_OUTPUT_MAX),
        "output_clamp_low": config.get(c.CONF_OUT_CLAMP_LOW),
        "output_clamp_high": config.get(c.CONF_OUT_CLAMP_HIGH),
        "kp": config.get(c.CONF_KP),
        "ki": config.get(c.CONF_KI),
        "kd": config.get(c.CONF_KD),
        "ke": config.get(c.CONF_KE),
        "pwm": pwm,
        "boost_pid_off": config.get(c.CONF_BOOST_PID_OFF),
        "autotune": config.get(c.CONF_AUTOTUNE),
        "noiseband": config.get(c.CONF_NOISEBAND),
        "lookback": config.get(c.CONF_LOOKBACK),
        c.CONF_DEBUG: config.get(c.CONF_DEBUG),
        # sync thermostat specific
        CONF_INPUT_DEVICE: config[CONF_INPUT_DEVICE],
        PARAM_HEATER_VALVES: heater_valves,
    }

    async_add_entities([SyncThermostat(**parameters)])

    # Same entity services as the original integration, under our domain.
    platform.async_register_entity_service(  # type: ignore
        "set_pid_gain",
        {
            vol.Optional("kp"): vol.Coerce(float),
            vol.Optional("ki"): vol.Coerce(float),
            vol.Optional("kd"): vol.Coerce(float),
            vol.Optional("ke"): vol.Coerce(float),
        },
        "async_set_pid",
    )
    platform.async_register_entity_service(  # type: ignore
        "set_pid_mode",
        {vol.Required("mode"): vol.In(["auto", "off"])},
        "async_set_pid_mode",
    )
    preset_schema: dict[Any, Any] = {}
    for preset in (
        "away",
        "eco",
        "boost",
        "comfort",
        "home",
        "sleep",
        "activity",
    ):
        preset_schema[vol.Optional(f"{preset}_temp")] = vol.Coerce(float)
        preset_schema[vol.Optional(f"{preset}_temp_disable")] = vol.Coerce(bool)
    platform.async_register_entity_service(  # type: ignore
        "set_preset_temp", preset_schema, "async_set_preset_temp"
    )
    platform.async_register_entity_service(  # type: ignore
        "clear_integral", {}, "clear_integral"
    )


class SyncThermostat(SmartThermostat):
    """SmartThermostat that mirrors a physical climate entity (TRV)."""

    def __init__(self, **kwargs):
        """Initialize the sync thermostat."""
        input_device: dict[str, Any] = kwargs.pop(CONF_INPUT_DEVICE)
        self._physical_entity_id: str = input_device[CONF_ENTITY]
        self._cooling_time: float = input_device[CONF_COOLDOWN_TIME].total_seconds()
        valves: dict[str, Any] | None = kwargs.pop(PARAM_HEATER_VALVES)
        self._heater_dict_mode: bool = valves is not None
        self._trv_valve_opening: list[str] = valves[CONF_VALVE_OPENING] if valves else []
        self._trv_valve_closing: list[str] = valves[CONF_VALVE_CLOSING] if valves else []
        self._trv_valve_min: float = valves[CONF_VALVE_MIN] if valves else 0.0
        self._trv_valve_lock = asyncio.Lock()
        # Internal integer heater (0-100): the value smart_thermostat would write
        # to a heater input_number; drives hvac_action and the valve degrees.
        self._heater_value: int = 0

        # This entity is only available while the physical thermostat is.
        self._physical_available = False
        # Last state written while available (see LAST_AVAILABLE_STATE).
        self._last_available_state: dict[str, Any] | None = None

        self._physical_min_temp: float | None = None
        self._pre_off_target_temp: float | None = None
        self._startup_synced = False
        self._in_operation = 0
        self._cooling_unsub = None
        self._pending: dict[str, Any] | None = None
        self._pending_unsub = None

        super().__init__(**kwargs)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def async_added_to_hass(self):
        """Restore state, then start listening to the physical thermostat."""
        # Known before the parent restores and runs its first control cycle.
        self._physical_available = self._is_available_state(
            self.hass.states.get(self._physical_entity_id)
        )

        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            self._pre_off_target_temp = self._parse_temp(
                old_state.attributes.get(ATTR_PRE_OFF_TARGET_TEMP)
            )

        self.async_on_remove(self._async_cancel_timers)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._physical_entity_id], self._async_physical_changed
            )
        )

        physical = self.hass.states.get(self._physical_entity_id)
        if physical is not None:
            self._update_physical_min_temp(physical)

        if self.hass.state == CoreState.running:
            await self._async_initial_push()
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START, self._async_on_ha_start
            )

    async def async_get_last_state(self) -> State | None:
        """Last stored state; if it was unavailable, the last available one instead."""
        state = await super().async_get_last_state()
        if state is None or state.state != STATE_UNAVAILABLE:
            return state
        extra = await self.async_get_last_extra_data()
        snapshot = (extra.as_dict() if extra is not None else {}).get(
            LAST_AVAILABLE_STATE
        )
        if not snapshot:
            return state
        restored = State.from_dict(snapshot)
        if restored is None:
            return state
        # Keep carrying it in case we restart again before becoming available.
        self._last_available_state = snapshot
        return restored

    @property
    def extra_restore_state_data(self) -> RestoredExtraData | None:
        """Store the last state written while available next to the regular one."""
        if self.hass is not None and self.entity_id:
            current = self.hass.states.get(self.entity_id)
            if current is not None and current.state != STATE_UNAVAILABLE:
                self._last_available_state = dict(current.as_dict())
        if self._last_available_state is None:
            return None
        return RestoredExtraData({LAST_AVAILABLE_STATE: self._last_available_state})

    async def _async_on_ha_start(self, _event) -> None:
        await self._async_initial_push()

    async def _async_initial_push(self) -> None:
        """At startup the sync thermostat is the source: push its state to the TRV."""
        if self._startup_synced:
            return
        physical = self.hass.states.get(self._physical_entity_id)
        if physical is None or physical.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return  # will retry when the physical entity becomes available
        self._startup_synced = True
        await self._async_push_to_physical()

    @callback
    def _async_cancel_timers(self) -> None:
        self._async_cancel_cooling_timer()
        self._async_clear_pending()

    @callback
    def _async_cancel_cooling_timer(self) -> None:
        if self._cooling_unsub is not None:
            self._cooling_unsub()
            self._cooling_unsub = None

    @callback
    def _async_clear_pending(self) -> None:
        if self._pending_unsub is not None:
            self._pending_unsub()
            self._pending_unsub = None
        self._pending = None

    @callback
    def _async_schedule_apply(self) -> None:
        """(Re)start the cooling timer that applies the physical state to us."""
        self._async_cancel_cooling_timer()
        self._cooling_unsub = async_call_later(
            self.hass, self._cooling_time, self._async_apply_physical
        )

    # ------------------------------------------------------------------ #
    # Availability (follows the physical thermostat)
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """Unavailable while the physical thermostat is missing or unavailable."""
        return self._physical_available

    @staticmethod
    def _is_available_state(state: State | None) -> bool:
        return state is not None and state.state != STATE_UNAVAILABLE

    async def _async_control_heating(self, time_func=None, calc_pid=False):
        """Pause the control loop (PID, heater, valves) while unavailable."""
        if not self._physical_available:
            return None
        return await super()._async_control_heating(time_func, calc_pid)

    async def _async_physical_became_unavailable(self) -> None:
        """Stop syncing and put the heater in a safe (off) state."""
        _LOGGER.warning(
            "%s: %s is unavailable; pausing control",
            self.entity_id,
            self._physical_entity_id,
        )
        self._async_cancel_timers()
        async with self._temp_lock:
            # Don't integrate over the unavailable period once we resume.
            self._previous_temp = None
            self._previous_temp_time = None
            if self._pid_controller is not None:
                self._pid_controller.clear_samples()
            try:
                if self._heater_dict_mode:
                    # The valves belong to the unreachable TRV: nothing to write.
                    self._heater_value = 0
                elif self._pwm:
                    await self._async_heater_turn_off(force=True)
                else:
                    self._control_output = self._output_min
                    await self._async_set_valve_value(self._control_output)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "%s: turning the heater off failed", self.entity_id
                )
        self.async_write_ha_state()

    async def _async_physical_became_available(self) -> None:
        """Resume: like at startup, our state is pushed to the physical thermostat."""
        _LOGGER.info(
            "%s: %s is available again; resuming control",
            self.entity_id,
            self._physical_entity_id,
        )
        self.async_write_ha_state()
        if self.hass.state == CoreState.running:
            self._startup_synced = False
            await self._async_initial_push()
        await self._async_control_heating(calc_pid=True)

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #
    @property
    def min_temp(self):
        """Config min_temp, else the physical thermostat's min_temp, else default."""
        if self._min_temp:
            return self._min_temp
        if self._physical_min_temp is not None:
            return self._physical_min_temp
        return super().min_temp

    @property
    def extra_state_attributes(self):
        """Add sync-specific attributes."""
        attrs = dict(super().extra_state_attributes)
        attrs[ATTR_PRE_OFF_TARGET_TEMP] = self._pre_off_target_temp
        attrs[ATTR_PHYSICAL_THERMOSTAT] = self._physical_entity_id
        if self._heater_dict_mode:
            attrs["heater_value"] = self._heater_value
        return attrs

    # ------------------------------------------------------------------ #
    # Sync thermostat -> physical (no debounce)
    # ------------------------------------------------------------------ #
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode. Leaving OFF restores the target from before OFF.

        Entering OFF keeps the target as is: the physical thermostat reports its own
        off setpoint (e.g. frost protection) and that value is then mirrored here.
        """
        self._in_operation += 1
        try:
            previous = self._hvac_mode
            if hvac_mode == HVACMode.OFF and previous != HVACMode.OFF:
                if self._target_temp is not None:
                    self._pre_off_target_temp = self._target_temp
            elif previous == HVACMode.OFF and hvac_mode != HVACMode.OFF:
                if self._pre_off_target_temp is not None:
                    self._target_temp = self._pre_off_target_temp
            await super().async_set_hvac_mode(hvac_mode)
        finally:
            self._in_operation -= 1
        self.async_write_ha_state()
        await self._async_push_to_physical()

    async def async_set_temperature(self, **kwargs) -> None:
        """Set target temperature. While OFF this also switches to heat."""
        if kwargs.get(ATTR_TEMPERATURE) is None:
            return
        self._in_operation += 1
        try:
            if self._hvac_mode == HVACMode.OFF:
                await super().async_set_hvac_mode(HVACMode.HEAT)
            await super().async_set_temperature(**kwargs)
        finally:
            self._in_operation -= 1
        await self._async_push_to_physical()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Presets change the target temperature, so push it as well."""
        await super().async_set_preset_mode(preset_mode)
        if not self._in_operation:
            await self._async_push_to_physical()

    async def _async_push_to_physical(self) -> None:
        """Write the sync thermostat's mode/target to the physical thermostat."""
        physical = self.hass.states.get(self._physical_entity_id)
        if physical is None or physical.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        # The sync thermostat is now the truth; drop any pending physical apply.
        self._async_cancel_cooling_timer()

        mode = self._hvac_mode
        mode_str = mode.value if isinstance(mode, HVACMode) else (str(mode) if mode else None)
        target = self._target_temp
        physical_modes = [str(m) for m in (physical.attributes.get(ATTR_HVAC_MODES) or [])]
        physical_temp = self._parse_temp(physical.attributes.get(ATTR_TEMPERATURE))
        mode_supported = mode_str in physical_modes
        temp_differs = target is not None and (
            physical_temp is None or abs(physical_temp - target) > TEMP_TOLERANCE
        )

        calls: list[tuple[str, dict[str, Any]]] = []
        expected_mode: str | None = None
        expected_temp: float | None = None
        wait_for_mode = False

        if mode_str == HVACMode.OFF.value:
            # Only the mode: the device applies its own off setpoint, which is then
            # mirrored back. Never write a setpoint around OFF (it would either turn
            # the device back on or overwrite its off temperature).
            if mode_supported and physical.state != HVACMode.OFF.value:
                calls.append((SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: mode_str}))
                expected_mode = mode_str
        else:
            if mode_supported and physical.state != mode_str:
                calls.append((SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: mode_str}))
                expected_mode = mode_str
                wait_for_mode = physical.state == HVACMode.OFF.value
            if temp_differs and (mode_supported or physical.state != HVACMode.OFF.value):
                calls.append((SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: target}))
                expected_temp = target

        if not calls:
            return

        self._async_clear_pending()
        self._pending = {"mode": expected_mode, "temp": expected_temp}
        for service, data in calls:
            await self._async_call_physical(service, data)
            if service == SERVICE_SET_HVAC_MODE and wait_for_mode and len(calls) > 1:
                await self._async_wait_physical_mode(mode_str)
        # Echo window starts once every command has been sent.
        if self._pending is not None:
            self._pending_unsub = async_call_later(
                self.hass, PUSH_ECHO_TIMEOUT, self._async_pending_expired
            )

    async def _async_wait_physical_mode(self, mode: str) -> None:
        """Wait until the physical thermostat reports `mode` (bounded)."""
        state = self.hass.states.get(self._physical_entity_id)
        if state is not None and state.state == mode:
            return
        reached = asyncio.Event()

        @callback
        def _check(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is not None and new_state.state == mode:
                reached.set()

        unsub = async_track_state_change_event(
            self.hass, [self._physical_entity_id], _check
        )
        try:
            await asyncio.wait_for(reached.wait(), PHYSICAL_MODE_WAIT)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "%s: %s did not report %s within %ss; sending the setpoint anyway",
                self.entity_id,
                self._physical_entity_id,
                mode,
                PHYSICAL_MODE_WAIT,
            )
        finally:
            unsub()

    async def _async_call_physical(self, service: str, data: dict[str, Any]) -> None:
        payload = {ATTR_ENTITY_ID: self._physical_entity_id, **data}
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN, service, payload, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "%s: calling climate.%s on %s failed",
                self.entity_id,
                service,
                self._physical_entity_id,
            )

    async def _async_pending_expired(self, _now) -> None:
        """Stop treating physical events as echoes of our own command."""
        self._pending_unsub = None
        self._pending = None

    # ------------------------------------------------------------------ #
    # Physical -> sync thermostat (cooling time / settle timer)
    # ------------------------------------------------------------------ #
    @callback
    def _async_physical_changed(self, event: Event[EventStateChangedData]) -> None:
        """Physical thermostat changed: (re)start the cooling timer."""
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]

        if new_state is not None:
            self._update_physical_min_temp(new_state)

        available = self._is_available_state(new_state)
        if available != self._physical_available:
            self._physical_available = available
            if available:
                self.hass.async_create_task(self._async_physical_became_available())
            else:
                self.hass.async_create_task(self._async_physical_became_unavailable())
            return

        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        if not self._startup_synced:
            self.hass.async_create_task(self._async_initial_push())
            return

        if self._pending is not None:
            # Events right after our own write are echoes/intermediate values.
            if self._pending_matches(new_state):
                self._async_clear_pending()
                # The echo may carry values we did not command (e.g. the device's
                # own off setpoint): read the device once it settles.
                self._async_schedule_apply()
            return

        if old_state is not None and self._signature(old_state) == self._signature(
            new_state
        ):
            return  # only e.g. current_temperature changed

        self._async_schedule_apply()

    async def _async_apply_physical(self, _now) -> None:
        """Cooling time elapsed: read the physical entity and apply it verbatim."""
        self._cooling_unsub = None
        physical = self.hass.states.get(self._physical_entity_id)
        if physical is None or physical.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        mode = self._parse_physical_mode(physical.state)  # None for e.g. "auto"
        temp = self._parse_temp(physical.attributes.get(ATTR_TEMPERATURE))

        changed = False
        self._in_operation += 1
        try:
            if mode is not None and mode != self._hvac_mode:
                if (
                    mode == HVACMode.OFF
                    and self._hvac_mode != HVACMode.OFF
                    and self._target_temp is not None
                ):
                    # Remember the target so a later heat from our side restores it.
                    self._pre_off_target_temp = self._target_temp
                await super().async_set_hvac_mode(mode)
                changed = True
            if temp is not None and (
                self._target_temp is None
                or abs(temp - self._target_temp) > TEMP_TOLERANCE
            ):
                await super().async_set_temperature(**{ATTR_TEMPERATURE: temp})
                changed = True
        finally:
            self._in_operation -= 1
        if changed:
            self.async_write_ha_state()

    # ------------------------------------------------------------------ #
    # Heater as valve mapping: internal heater value -> valve opening/closing
    # ------------------------------------------------------------------ #
    @property
    def _is_device_active(self):
        """In valve mapping mode "heating" comes from the internal heater value."""
        if self._heater_dict_mode:
            return self._heater_value > 0
        return super()._is_device_active

    async def _async_set_valve_value(self, value: float):
        """Handle the heater output (0-100) computed by the PID."""
        if not self._heater_dict_mode:
            await super()._async_set_valve_value(value)
            return
        self._heater_value = max(0, min(100, int(value)))
        _LOGGER.debug("%s: heater value %s", self.entity_id, self._heater_value)
        await self._async_apply_valve_degrees()

    async def _async_heater_turn_on(self):
        if self._heater_dict_mode:
            return  # there is no switch to toggle (pwm is always 0)
        await super()._async_heater_turn_on()

    async def _async_heater_turn_off(self, force=False):
        if self._heater_dict_mode:
            return  # there is no switch to toggle (pwm is always 0)
        await super()._async_heater_turn_off(force=force)

    async def _async_apply_valve_degrees(self) -> None:
        """Scale the heater value by valve_min and write the valve numbers."""
        scale = (100 - self._trv_valve_min) / 100
        opening = int(round(self._trv_valve_min + self._heater_value * scale, 6))
        closing = 100 - opening
        async with self._trv_valve_lock:
            for entity_ids, value in (
                (self._trv_valve_opening, opening),
                (self._trv_valve_closing, closing),
            ):
                # Only write entities that don't already hold the value, so the
                # TRV isn't hit on every PID cycle.
                todo = [e for e in entity_ids if not self._number_is(e, value)]
                if not todo:
                    continue
                try:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {ATTR_ENTITY_ID: todo, "value": value},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception(
                        "%s: setting %s to %s failed", self.entity_id, todo, value
                    )

    def _number_is(self, entity_id: str, value: float) -> bool:
        state = self.hass.states.get(entity_id)
        current = self._parse_temp(state.state) if state is not None else None
        return current is not None and abs(current - value) < 0.01

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _pending_matches(self, state) -> bool:
        pending = self._pending or {}
        if pending.get("mode") is not None and state.state != pending["mode"]:
            return False
        if pending.get("temp") is not None:
            temp = self._parse_temp(state.attributes.get(ATTR_TEMPERATURE))
            if temp is None or abs(temp - pending["temp"]) > self._tolerance(state):
                return False
        return True

    def _tolerance(self, state) -> float:
        """Allow for the TRV rounding to its own step."""
        step = self._parse_temp(state.attributes.get(ATTR_TARGET_TEMP_STEP))
        return max(TEMP_TOLERANCE, step / 2 + 0.01) if step else TEMP_TOLERANCE

    def _parse_physical_mode(self, state: str) -> HVACMode | None:
        """Map the physical state to an HVAC mode we support; ignore the rest."""
        try:
            mode = HVACMode(state)
        except ValueError:
            return None
        return mode if mode in self.hvac_modes else None

    def _update_physical_min_temp(self, state) -> None:
        value = self._parse_temp(state.attributes.get(ATTR_MIN_TEMP))
        if value is not None:
            self._physical_min_temp = value

    @staticmethod
    def _signature(state) -> tuple[Any, Any]:
        return state.state, state.attributes.get(ATTR_TEMPERATURE)

    @staticmethod
    def _parse_temp(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
