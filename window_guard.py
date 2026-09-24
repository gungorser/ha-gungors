"""A cover entity that wraps a raw blind and refuses remote movement while a window is open.

Design summary (see gungors/Customizations README for the full writeup):

- The wrapped ("raw") cover is the physical IKEA blind entity (e.g. a
  Zigbee2MQTT cover). This entity shows a *setpoint* position instead of the
  raw entity's position.
- Any position report from the raw cover that we did not ask for ourselves
  is treated as physical: the setpoint is snapped to it immediately, and
  `is_sync` stays true throughout.
- A remote command (open/close/set_position from the UI, an automation,
  voice, etc.) updates the setpoint. If the window is closed we forward the
  command to the raw cover immediately. If the window is open we hold the
  command: the raw cover is not touched, and `is_sync` becomes false until
  the window closes.
- Unbound physical buttons are handled by the pushbutton blueprint, which
  fires a `gungors_physical_cover` event for window_guard covers instead of
  calling cover services. This entity moves the raw cover on that event
  without checking the window. Zigbee-bound buttons bypass HA entirely, so
  the rule for everything else is deliberately simple: *any* movement of the
  raw cover that we did not command ourselves is treated as physical and
  always allowed, window state notwithstanding.
- IKEA/Zigbee2MQTT blinds here do not publish an opening/closing state --
  only a slowly-changing position. This entity derives is_opening/is_closing
  from consecutive position reports so the UI and automations can use it,
  something the raw entity cannot offer.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ATTR_ACTUAL_POSITION,
    ATTR_IS_SYNC,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_SILENCE,
    EVENT_PHYSICAL_COVER,
    MAX_REAL_STEP,
    POSITION_TOLERANCE,
)

_LOGGER = logging.getLogger(__name__)

# Internal movement bookkeeping states.
_IDLE = "idle"
_COMMANDED = "commanded"
_PHYSICAL = "physical"
_STOPPING = "stopping"  # we sent a stop; trailing reports are not physical


def _window_is_open(hass: HomeAssistant, window_entity_id: str) -> bool:
    """Return True if the window should be considered open (fail safe)."""
    state = hass.states.get(window_entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return True
    # device_class door/window/opening convention: "on" = open, "off" = closed.
    return state.state != "off"


def _position_of(state) -> int | None:
    """Current position of a cover state, or None if unavailable/unknown."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    pos = state.attributes.get(ATTR_CURRENT_POSITION)
    if pos is None:
        return None
    try:
        return int(pos)
    except (TypeError, ValueError):
        return None


class WindowGuardedCover(CoverEntity, RestoreEntity):
    """Cover entity guarding a raw blind against remote movement while a window is open."""

    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        name: str,
        unique_id: str,
        raw_cover_entity_id: str,
        window_entity_id: str,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        stop_silence: float = DEFAULT_STOP_SILENCE,
    ) -> None:
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._raw_entity_id = raw_cover_entity_id
        self._window_entity_id = window_entity_id
        self._start_timeout = start_timeout
        self._stop_silence = stop_silence

        # Setpoint: what we show as current_cover_position and what we'll
        # push to the raw cover once the window allows it.
        self._setpoint: int | None = None

        # Bookkeeping for the raw cover's real position/direction.
        self._actual_position: int | None = None
        self._last_reported_position: int | None = None
        self._movement_state = _IDLE  # idle / commanded / physical / stopping
        self._is_opening = False
        self._is_closing = False

        # Commanded-move bookkeeping.
        self._commanded_target: int | None = None
        self._commanded_retried = False

        # A command is pending because the window was open when it was issued.
        self._pending_setpoint: int | None = None

        # Set on restart when the last saved state had a pending (unsynced)
        # command; resolved on the first valid raw position report.
        self._restored_pending = False
        self._baseline_needed = True

        self._stall_unsub = None
        self._start_timeout_unsub = None

    # ------------------------------------------------------------------
    # Home Assistant lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored = last_state.attributes.get(ATTR_CURRENT_POSITION)
            if restored is not None:
                try:
                    self._setpoint = int(restored)
                except (TypeError, ValueError):
                    self._setpoint = None
            # Only a pending (unsynced) command survives a restart. If we were
            # in sync, the raw blind's real position is the truth (it may have
            # been moved by hand while HA was down).
            self._restored_pending = last_state.attributes.get(ATTR_IS_SYNC) is False

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._raw_entity_id], self._handle_raw_state_change
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._window_entity_id], self._handle_window_change
            )
        )

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_PHYSICAL_COVER, self._handle_physical_event)
        )

        raw_state = self.hass.states.get(self._raw_entity_id)
        position = _position_of(raw_state)
        if position is not None:
            await self._establish_baseline(position)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending timers so they don't fire on a removed entity (reload)."""
        self._cancel_timers()
        await super().async_will_remove_from_hass()

    async def _establish_baseline(self, position: int) -> None:
        """First valid raw position after startup: decide what the setpoint is."""
        self._baseline_needed = False
        self._actual_position = position
        self._last_reported_position = position

        if self._restored_pending and self._setpoint is not None and self._setpoint != position:
            if _window_is_open(self.hass, self._window_entity_id):
                self._pending_setpoint = self._setpoint
                self.async_write_ha_state()
            else:
                await self._send_to_raw(self._setpoint)
        else:
            self._setpoint = position
            self.async_write_ha_state()
        self._restored_pending = False

    @callback
    def _handle_physical_event(self, event: Event) -> None:
        """Physical button pressed (via the pushbutton blueprint's event)."""
        ids = event.data.get("entity_id", [])
        if isinstance(ids, str):
            ids = [ids]
        action = event.data.get("action")
        if self.entity_id in ids and action in ("open", "close"):
            self.hass.async_create_task(self._handle_physical_button(action))

    async def _handle_physical_button(self, direction: str) -> None:
        """A physical button was pressed: always allowed, no window check."""
        self._movement_state = _PHYSICAL
        self._pending_setpoint = None  # physical decision overrides any held command
        self._commanded_target = None
        self._cancel_timers()
        if direction == "open":
            await self.hass.services.async_call(
                "cover", "open_cover", {ATTR_ENTITY_ID: self._raw_entity_id}, blocking=False
            )
        else:
            await self.hass.services.async_call(
                "cover", "close_cover", {ATTR_ENTITY_ID: self._raw_entity_id}, blocking=False
            )
        self._arm_stall_timer()

    # ------------------------------------------------------------------
    # CoverEntity API
    # ------------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        return self._setpoint

    @property
    def is_closed(self) -> bool | None:
        if self._setpoint is None:
            return None
        return self._setpoint <= 0

    @property
    def is_opening(self) -> bool:
        return self._movement_state != _IDLE and self._is_opening

    @property
    def is_closing(self) -> bool:
        return self._movement_state != _IDLE and self._is_closing

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        is_sync = (
            self._pending_setpoint is None
            and self._actual_position is not None
            and self._setpoint is not None
            and abs(self._actual_position - self._setpoint) <= POSITION_TOLERANCE
        )
        return {
            ATTR_IS_SYNC: is_sync,
            ATTR_ACTUAL_POSITION: self._actual_position,
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_request_position(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_request_position(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_request_position(int(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        # A remote stop cancels any pending/commanded move. The setpoint
        # settles on wherever the blind actually stops (see _handle_stall).
        self._pending_setpoint = None
        self._commanded_target = None
        self._cancel_timers()
        self._movement_state = _STOPPING
        self._is_opening = False
        self._is_closing = False
        await self.hass.services.async_call(
            "cover", "stop_cover", {ATTR_ENTITY_ID: self._raw_entity_id}, blocking=False
        )
        if self._actual_position is not None:
            self._setpoint = self._actual_position
        self._arm_stall_timer()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Remote command handling
    # ------------------------------------------------------------------

    async def _async_request_position(self, target: int) -> None:
        target = max(0, min(100, target))
        self._setpoint = target
        self.async_write_ha_state()

        if _window_is_open(self.hass, self._window_entity_id):
            self._pending_setpoint = target
            _LOGGER.debug(
                "%s: window open, holding setpoint %s", self.entity_id, target
            )
            return

        self._pending_setpoint = None
        await self._send_to_raw(target)

    async def _send_to_raw(self, target: int) -> None:
        if (
            self._actual_position is not None
            and abs(target - self._actual_position) <= POSITION_TOLERANCE
        ):
            # Already there: the blind won't move or report, so sending would
            # only end in a start_timeout retry and a false "no response".
            self._finish_move(self._actual_position)
            return
        self._commanded_target = target
        self._commanded_retried = False
        self._movement_state = _COMMANDED
        if self._actual_position is not None:
            self._is_opening = target > self._actual_position
            self._is_closing = target < self._actual_position
        self.async_write_ha_state()

        await self.hass.services.async_call(
            "cover",
            "set_cover_position",
            {ATTR_ENTITY_ID: self._raw_entity_id, ATTR_POSITION: target},
            blocking=False,
        )
        self._arm_start_timeout()

    # ------------------------------------------------------------------
    # Window tracking
    # ------------------------------------------------------------------

    @callback
    def _handle_window_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        window_open = _window_is_open(self.hass, self._window_entity_id)

        if window_open and self._movement_state == _COMMANDED:
            # Window opened mid-move: stop the raw cover, keep the setpoint.
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "cover", "stop_cover", {ATTR_ENTITY_ID: self._raw_entity_id}, blocking=False
                )
            )
            self._pending_setpoint = self._setpoint
            self._commanded_target = None
            self._cancel_timers()
            self._movement_state = _STOPPING
            self._is_opening = False
            self._is_closing = False
            self._arm_stall_timer()
            self.async_write_ha_state()
            return

        if not window_open and self._pending_setpoint is not None:
            target = self._pending_setpoint
            self._pending_setpoint = None
            self.hass.async_create_task(self._send_to_raw(target))

    # ------------------------------------------------------------------
    # Raw cover tracking
    # ------------------------------------------------------------------

    @callback
    def _handle_raw_state_change(self, event: Event) -> None:
        position = _position_of(event.data.get("new_state"))
        if position is None:
            return

        if self._baseline_needed:
            self.hass.async_create_task(self._establish_baseline(position))
            return

        previous = self._actual_position
        self._actual_position = position

        if self._movement_state == _STOPPING:
            # Trailing reports after our own stop: track, don't treat as physical.
            self._arm_stall_timer()
            self.async_write_ha_state()
            return

        if self._movement_state == _COMMANDED and self._commanded_target is not None:
            if (
                previous is not None
                and position == self._commanded_target
                and abs(position - previous) > MAX_REAL_STEP
            ):
                # Z2M echo of our own target, not a real position. Ignore it
                # and keep tracking from the last real position.
                self._actual_position = previous
                return
            self._cancel_start_timeout()  # we got a report, no need to retry
            if abs(position - self._commanded_target) <= POSITION_TOLERANCE:
                self._finish_move(position)
                return
            # Still moving. A direction reversal means someone physically
            # overrode us mid-move.
            if previous is not None and position != previous:
                expected_opening = self._commanded_target > previous
                actually_opening = position > previous
                if expected_opening != actually_opening:
                    self._commanded_target = None
                    self._movement_state = _PHYSICAL
                    self._setpoint = position
                    self._is_opening = actually_opening
                    self._is_closing = not actually_opening
                    self._arm_stall_timer()
                    self.async_write_ha_state()
                    return
            self._is_opening = self._commanded_target > position
            self._is_closing = self._commanded_target < position
            self._last_reported_position = position
            self._arm_stall_timer()
            self.async_write_ha_state()
            return

        if self._movement_state == _IDLE and position == previous:
            return  # attribute-only update, nothing moved

        # Not something we commanded: physical movement (bound button, unbound
        # button, or anything else moving the raw entity). Physical wins:
        # setpoint follows and any held remote command is dropped.
        self._movement_state = _PHYSICAL
        self._pending_setpoint = None
        self._setpoint = position
        if previous is not None and position != previous:
            self._is_opening = position > previous
            self._is_closing = position < previous
        self._last_reported_position = position
        self._arm_stall_timer()
        self.async_write_ha_state()

    def _finish_move(self, final_position: int) -> None:
        self._setpoint = final_position
        self._movement_state = _IDLE
        self._is_opening = False
        self._is_closing = False
        self._commanded_target = None
        self._cancel_timers()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _arm_stall_timer(self) -> None:
        self._cancel_stall_timer()
        self._stall_unsub = async_call_later(
            self.hass, self._stop_silence, self._handle_stall
        )

    @callback
    def _handle_stall(self, _now) -> None:
        """No new position report for stop_silence seconds: treat as stopped."""
        self._stall_unsub = None
        if self._actual_position is not None and self._pending_setpoint is None:
            self._setpoint = self._actual_position
        self._movement_state = _IDLE
        self._is_opening = False
        self._is_closing = False
        self._commanded_target = None
        self.async_write_ha_state()

    def _arm_start_timeout(self) -> None:
        self._cancel_start_timeout()
        self._start_timeout_unsub = async_call_later(
            self.hass, self._start_timeout, self._handle_start_timeout
        )

    @callback
    def _handle_start_timeout(self, _now) -> None:
        """No position report at all after issuing a command."""
        self._start_timeout_unsub = None
        if self._commanded_target is None:
            return
        if not self._commanded_retried:
            self._commanded_retried = True
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "cover",
                    "set_cover_position",
                    {ATTR_ENTITY_ID: self._raw_entity_id, ATTR_POSITION: self._commanded_target},
                    blocking=False,
                )
            )
            self._arm_start_timeout()
            return
        # Gave up: leave the setpoint as requested, is_sync will read false
        # until something (a report, or the user) resolves it.
        _LOGGER.warning(
            "%s: no response from %s after retry, giving up on command",
            self.entity_id,
            self._raw_entity_id,
        )
        self._movement_state = _IDLE
        self._is_opening = False
        self._is_closing = False
        self.async_write_ha_state()

    def _cancel_stall_timer(self) -> None:
        if self._stall_unsub is not None:
            self._stall_unsub()
            self._stall_unsub = None

    def _cancel_start_timeout(self) -> None:
        if self._start_timeout_unsub is not None:
            self._start_timeout_unsub()
            self._start_timeout_unsub = None

    def _cancel_timers(self) -> None:
        self._cancel_stall_timer()
        self._cancel_start_timeout()
