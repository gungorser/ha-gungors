"""A cover entity for Tuya curtain motors that only report position at the end.

Why this exists (observed on the salon Tuya curtain via Zigbee2MQTT):

- The motor sends no position updates while moving and has no reliable
  moving state (Z2M's `running` is only true in the echo of a position
  command and is cleared by Z2M itself ~3 s later).
- Real position (Tuya dp3) arrives only when a move ends by itself or when
  it is stopped. A position command is first echoed back as the target
  (dp2, `running: true`) before the curtain has moved.
- After a power loss the motor keeps its direction but loses calibration:
  the first full run ends with a bogus value that Z2M publishes as the end
  opposite to the command. From the next run on reports are correct again.
- The motor's "open" is the room's "close". Z2M's `invert_cover` option
  can't fix that for this device: it only flips reported/commanded
  positions, not the open/close commands. So with `invert: true` this entity
  flips everything itself (open <-> close, p <-> 100 - p, reports too),
  like the old template did. Z2M's own option is left alone.

This entity therefore:
- works in the room's frame (see invert above),
- estimates position while moving from a learned full-run time per motor
  direction (open and close can differ) and shows opening/closing,
- learns those times from every move of at least 30% that starts and ends
  on a real report (including stopped moves): time from command to report,
  scaled to a full run. They start at 10 s and are kept in the motor's
  frame, so changing `invert` swaps them in the room's frame,
- replaces the estimate with a real report whenever one arrives,
- listens to the Z2M device topic directly (HA's MQTT cover drops repeated
  identical positions, which would hide the arrival report after an echo),
- ignores echoes and the post-power-loss bogus end report (`calibrated`
  becomes false until the next normal end report).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity, RestoredExtraData

from .const import (
    ATTR_CALIBRATED,
    ATTR_CLOSE_TIME,
    ATTR_OPEN_TIME,
    ATTR_POSITION_SOURCE,
    DEFAULT_TRAVEL_TIME,
    DEFAULT_Z2M_BASE_TOPIC,
    MIN_LEARN_DISTANCE,
    POSITION_TOLERANCE,
)

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(milliseconds=500)
STOP_REPORT_WAIT = 3  # s to wait for the real position after a stop
COMMAND_GRACE = 1.0  # s after a command in which stray reports are ignored
REPUBLISH_WINDOW = 3.8  # s after a position echo in which Z2M re-publishes it
SOURCE_REPORTED = "reported"
SOURCE_ESTIMATED = "estimated"

# What the entity is currently doing.
_IDLE = "idle"
_MOVING = "moving"
_STOPPING = "stopping"

# Kind of command that started the current move.
_CMD_OPEN = "open"
_CMD_CLOSE = "close"
_CMD_POSITION = "position"


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= POSITION_TOLERANCE


class TimedCurtainCover(CoverEntity, RestoreEntity):
    """Curtain whose position is estimated in motion and corrected by reports."""

    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.CURTAIN
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
        invert: bool = False,
        z2m_base_topic: str = DEFAULT_Z2M_BASE_TOPIC,
    ) -> None:
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._raw_entity_id = raw_cover_entity_id
        self._invert = invert
        # Learned full-run times (s), in the motor's frame.
        self._motor_open_time = float(DEFAULT_TRAVEL_TIME)
        self._motor_close_time = float(DEFAULT_TRAVEL_TIME)
        self._base_topic = z2m_base_topic.rstrip("/")

        self._position: float | None = None
        self._source = SOURCE_REPORTED
        self._calibrated = True

        self._state = _IDLE
        self._cmd: str | None = None
        self._target: float = 0
        self._start_pos: float = 0
        self._start_time: float = 0.0
        self._expected: float = 0.0  # expected duration of the current move (s)
        self._echo_at: float | None = None  # when the position echo arrived
        self._republish_skipped = False
        self._start_reported = False  # move started from a reported position

        # The wrapper is only available while the raw cover is.
        self._raw_available = False

        self._tick_unsub = None
        self._deadline_unsub = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last = await self.async_get_last_state()
        if last is not None:
            restored = last.attributes.get(ATTR_CURRENT_POSITION)
            if restored is not None:
                try:
                    self._position = float(restored)
                except (TypeError, ValueError):
                    self._position = None
            if last.attributes.get(ATTR_CALIBRATED) is False:
                self._calibrated = False
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            if last is None or last.state == STATE_UNAVAILABLE:
                # Saved while unavailable (a state without attributes).
                try:
                    self._position = float(data["position"])
                except (KeyError, TypeError, ValueError):
                    pass
                if data.get("calibrated") is False:
                    self._calibrated = False
            for key in ("motor_open_time", "motor_close_time"):
                try:
                    value = float(data[key])
                except (KeyError, TypeError, ValueError):
                    continue
                if value > 0:
                    setattr(self, f"_{key}", value)

        if self._position is None:
            raw = self.hass.states.get(self._raw_entity_id)
            if raw is not None and raw.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                pos = raw.attributes.get(ATTR_CURRENT_POSITION)
                if pos is not None:
                    self._position = self._flip(float(pos))

        raw = self.hass.states.get(self._raw_entity_id)
        self._raw_available = raw is not None and raw.state != STATE_UNAVAILABLE
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._raw_entity_id], self._handle_raw_state
            )
        )

        friendly = self._z2m_friendly_name()
        if friendly is None:
            _LOGGER.error(
                "%s: %s is not a Zigbee2MQTT device, cannot follow its reports",
                self.entity_id,
                self._raw_entity_id,
            )
            return

        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.error("%s: MQTT is not available", self.entity_id)
            return

        self.async_on_remove(
            await mqtt.async_subscribe(
                self.hass, f"{self._base_topic}/{friendly}", self._handle_mqtt
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timers()
        await super().async_will_remove_from_hass()

    def _z2m_friendly_name(self) -> str | None:
        """Z2M friendly name of the raw cover's device (its MQTT topic)."""
        entry = er.async_get(self.hass).async_get(self._raw_entity_id)
        if entry is None or entry.device_id is None:
            return None
        device = dr.async_get(self.hass).async_get(entry.device_id)
        if device is None:
            return None
        if not any(
            domain == "mqtt" and ident.startswith("zigbee2mqtt_")
            for domain, ident in device.identifiers
        ):
            return None
        # Z2M publishes the device under its friendly name, which is the
        # device name it announces over discovery.
        return device.name

    def _flip(self, position: float) -> float:
        """Room frame <-> raw frame (the same operation both ways)."""
        return 100 - position if self._invert else position

    def _travel_time(self, room_opening: bool) -> float:
        """Learned full-run time for a move in the room's direction."""
        motor_opening = room_opening != self._invert
        return self._motor_open_time if motor_opening else self._motor_close_time

    def _learn(self, room_opening: bool, full_run: float) -> None:
        if room_opening != self._invert:
            self._motor_open_time = full_run
        else:
            self._motor_close_time = full_run

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        return RestoredExtraData(
            {
                "motor_open_time": self._motor_open_time,
                "motor_close_time": self._motor_close_time,
                "position": self._position,
                "calibrated": self._calibrated,
            }
        )

    # ------------------------------------------------------------------
    # CoverEntity API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Unavailable while the raw cover is missing or unavailable."""
        return self._raw_available

    @property
    def current_cover_position(self) -> int | None:
        return None if self._position is None else int(round(self._position))

    @property
    def is_closed(self) -> bool | None:
        if self._position is None:
            return None
        return self._position <= 0 and self._state == _IDLE

    @property
    def is_opening(self) -> bool:
        return self._state == _MOVING and self._target > self._start_pos

    @property
    def is_closing(self) -> bool:
        return self._state == _MOVING and self._target < self._start_pos

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_CALIBRATED: self._calibrated,
            ATTR_POSITION_SOURCE: self._source,
            ATTR_OPEN_TIME: round(self._travel_time(True), 1),
            ATTR_CLOSE_TIME: round(self._travel_time(False), 1),
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_move(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_move(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_move(int(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        if self._state != _MOVING:
            return
        self._update_estimate()
        self._stop_ticking()
        self._state = _STOPPING
        self.async_write_ha_state()
        await self._raw("stop_cover")
        # The motor reports where it stopped right away; if it doesn't,
        # keep the estimate.
        self._arm_deadline(STOP_REPORT_WAIT)

    # ------------------------------------------------------------------
    # Moving
    # ------------------------------------------------------------------

    async def _async_move(self, target: int) -> None:
        target = max(0, min(100, target))
        if self._state == _MOVING:
            self._update_estimate()
        current = self._position if self._position is not None else (100 - target)
        if self._state != _MOVING and _near(current, target):
            return  # already there: the motor would not move or report

        if target >= 100:
            cmd = _CMD_OPEN
            service, data = ("close_cover" if self._invert else "open_cover"), {}
        elif target <= 0:
            cmd = _CMD_CLOSE
            service, data = ("open_cover" if self._invert else "close_cover"), {}
        else:
            cmd = _CMD_POSITION
            service = "set_cover_position"
            data = {ATTR_POSITION: int(round(self._flip(target)))}

        # Only a move from a known (reported, resting) position can be learned from.
        self._start_reported = self._state == _IDLE and self._source == SOURCE_REPORTED
        self._cmd = cmd
        self._target = float(target)
        self._start_pos = float(current)
        self._start_time = time.monotonic()
        self._expected = abs(target - current) / 100 * self._travel_time(target > current)
        self._echo_at = None
        self._republish_skipped = False
        self._state = _MOVING
        self._position = float(current)
        self._source = SOURCE_ESTIMATED
        self._start_ticking()
        # Safety net: no report at all -> end at the target.
        self._arm_deadline(self._expected + self._travel_time(target > current) * 0.5 + 5)
        self.async_write_ha_state()
        await self._raw(service, data)

    def _update_estimate(self) -> None:
        if self._state != _MOVING:
            return
        elapsed = time.monotonic() - self._start_time
        step = elapsed / self._travel_time(self._target > self._start_pos) * 100
        if self._target >= self._start_pos:
            self._position = min(self._target, self._start_pos + step)
        else:
            self._position = max(self._target, self._start_pos - step)

    def _finish_reported(self, position: float) -> None:
        """Move ended on a real report: learn from it, then finish."""
        distance = abs(position - self._start_pos)
        if self._start_reported and distance >= MIN_LEARN_DISTANCE:
            elapsed = time.monotonic() - self._start_time
            opening = position > self._start_pos
            full_run = elapsed * 100 / distance
            _LOGGER.debug(
                "%s: learned %s time %.1fs (%.0f%% in %.1fs)",
                self.entity_id,
                "open" if opening else "close",
                full_run,
                distance,
                elapsed,
            )
            self._learn(opening, full_run)
        self._finish(position, SOURCE_REPORTED)

    def _finish(self, position: float, source: str) -> None:
        self._cancel_timers()
        self._state = _IDLE
        self._cmd = None
        self._position = float(position)
        self._source = source
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Reports from Zigbee2MQTT
    # ------------------------------------------------------------------

    @callback
    def _handle_raw_state(self, event: Event) -> None:
        """Follow the raw cover's availability (reports come from MQTT)."""
        new_state = event.data.get("new_state")
        available = new_state is not None and new_state.state != STATE_UNAVAILABLE
        if available == self._raw_available:
            return
        self._raw_available = available
        if not available:
            _LOGGER.warning(
                "%s: %s is unavailable", self.entity_id, self._raw_entity_id
            )
            # Stop estimating: no fake movement, no deadline "arrival".
            moving = self._state != _IDLE
            self._update_estimate()
            self._cancel_timers()
            if moving:
                self._source = SOURCE_ESTIMATED
            self._state = _IDLE
            self._cmd = None
            self.async_write_ha_state()
            return
        _LOGGER.info("%s: %s is available again", self.entity_id, self._raw_entity_id)
        pos = None
        if new_state.state != STATE_UNKNOWN:
            pos = new_state.attributes.get(ATTR_CURRENT_POSITION)
        if pos is not None:
            try:
                self._position = self._flip(float(pos))
                self._source = SOURCE_REPORTED
            except (TypeError, ValueError):
                pass
        self.async_write_ha_state()

    @callback
    def _handle_mqtt(self, msg) -> None:
        try:
            payload = json.loads(msg.payload)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        position = payload.get("position")
        if position is None:
            return
        try:
            position = float(position)
        except (TypeError, ValueError):
            return
        self._handle_report(self._flip(position), payload.get("running"))

    @callback
    def _handle_report(self, position: float, running: Any) -> None:
        if running is True:
            # Echo of a position command's target, not a real position.
            if self._state == _MOVING and self._cmd == _CMD_POSITION:
                self._echo_at = time.monotonic()
            return

        if self._state == _STOPPING:
            self._finish_reported(position)
            return

        if self._state != _MOVING:
            # Moved by something other than us (motor button, pulling the
            # curtain, another controller), or a late correction after stop.
            self._position = position
            self._source = SOURCE_REPORTED
            self.async_write_ha_state()
            return

        elapsed = time.monotonic() - self._start_time
        if elapsed < COMMAND_GRACE and not _near(position, self._target):
            # A leftover report from before this command (e.g. reversing
            # mid-move); a real stop can't come this fast.
            return

        if _near(position, self._target):
            if (
                self._cmd == _CMD_POSITION
                and self._echo_at is not None
                and not self._republish_skipped
                and time.monotonic() - self._echo_at < REPUBLISH_WINDOW
            ):
                # Z2M re-publishes the echoed target ~3 s after the echo with
                # running=false, before the real arrival report.
                self._republish_skipped = True
                return
            self._calibrated = True
            self._finish_reported(position)
            return

        opposite = 0.0 if self._cmd == _CMD_OPEN else 100.0
        if (
            self._cmd in (_CMD_OPEN, _CMD_CLOSE)
            and _near(position, opposite)
            and elapsed >= self._travel_time(self._cmd == _CMD_OPEN) * 0.5
        ):
            # After a power loss the first end report is bogus and comes out
            # as the opposite end. The curtain did reach the commanded end.
            _LOGGER.warning(
                "%s: end report contradicts the %s command, motor looks "
                "uncalibrated (power loss?); assuming it reached %s",
                self.entity_id,
                self._cmd,
                int(self._target),
            )
            self._calibrated = False
            self._finish(self._target, SOURCE_ESTIMATED)
            return

        # Anything else mid-move: it was stopped (e.g. by the motor's own
        # button) and this is where it actually is.
        self._finish_reported(position)

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _start_ticking(self) -> None:
        self._stop_ticking()
        self._tick_unsub = async_track_time_interval(self.hass, self._tick, TICK)

    def _stop_ticking(self) -> None:
        if self._tick_unsub is not None:
            self._tick_unsub()
            self._tick_unsub = None

    @callback
    def _tick(self, _now) -> None:
        self._update_estimate()
        self.async_write_ha_state()

    def _arm_deadline(self, seconds: float) -> None:
        if self._deadline_unsub is not None:
            self._deadline_unsub()
        self._deadline_unsub = async_call_later(self.hass, seconds, self._deadline)

    @callback
    def _deadline(self, _now) -> None:
        self._deadline_unsub = None
        if self._state == _MOVING:
            _LOGGER.warning(
                "%s: no position report from %s, assuming %s",
                self.entity_id,
                self._raw_entity_id,
                int(self._target),
            )
            self._finish(self._target, SOURCE_ESTIMATED)
        elif self._state == _STOPPING:
            self._finish(self._position, SOURCE_ESTIMATED)

    def _cancel_timers(self) -> None:
        self._stop_ticking()
        if self._deadline_unsub is not None:
            self._deadline_unsub()
            self._deadline_unsub = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _raw(self, service: str, data: dict[str, Any] | None = None) -> None:
        await self.hass.services.async_call(
            "cover",
            service,
            {ATTR_ENTITY_ID: self._raw_entity_id, **(data or {})},
            blocking=False,
        )
