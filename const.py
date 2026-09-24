"""Constants for the gungors integration."""
from datetime import timedelta

DOMAIN = "gungors"

# --- window_guard cover type ---
CONF_TYPE = "type"
TYPE_WINDOW_GUARD = "window_guard"

CONF_COVER = "cover"
CONF_WINDOW = "window"
CONF_START_TIMEOUT = "start_timeout"
CONF_STOP_SILENCE = "stop_silence"

DEFAULT_START_TIMEOUT = 8  # seconds to wait for the first position report after a command
DEFAULT_STOP_SILENCE = 3  # seconds of no reports (or reaching target) before we call it stopped

# Fired by the pushbutton blueprint for window_guard covers. Movement triggered
# through this event is physical: allowed regardless of window state.
# event_data: {entity_id: str | list[str], action: "open" | "close"}
EVENT_PHYSICAL_COVER = "gungors_physical_cover"

ATTR_IS_SYNC = "is_sync"
ATTR_ACTUAL_POSITION = "actual_position"

# Position reports within this tolerance of the target count as "reached".
POSITION_TOLERANCE = 1

# Zigbee2MQTT echoes the *target* position immediately after a
# set_cover_position command, before the blind has moved. Real movement is
# ~2-3 units per report (one report per second), so a jump larger than this
# straight to the commanded target is treated as that echo and ignored.
MAX_REAL_STEP = 5

# --- climate: sync thermostat ---
# `input_device`: the physical thermostat the sync thermostat is kept in sync with.
CONF_INPUT_DEVICE = "input_device"
CONF_ENTITY = "entity"
CONF_COOLDOWN_TIME = "cooldown_time"
DEFAULT_COOLDOWN_TIME = timedelta(seconds=1)
# State attribute exposing the input device's entity id.
ATTR_PHYSICAL_THERMOSTAT = "physical_thermostat"

# `heater` may be a dict instead of an entity id: the internal heater value
# (0-100, what smart_thermostat would write to an input_number) is then scaled
# onto the TRV's valve opening/closing degree number entities.
CONF_VALVE_OPENING = "valve_opening"
CONF_VALVE_CLOSING = "valve_closing"
CONF_VALVE_MIN = "valve_min"
DEFAULT_VALVE_MIN = 0

# Internal parameter name used to hand the parsed heater dict to the entity.
PARAM_HEATER_VALVES = "heater_valves"

# Extra state attribute used to remember the target temperature from before
# the thermostat was switched OFF (restored when switched back to heat).
ATTR_PRE_OFF_TARGET_TEMP = "pre_off_target_temp"

# How long (seconds) events from the physical thermostat are treated as echoes
# of a command we just sent, before we consider them real manual changes again.
PUSH_ECHO_TIMEOUT = 15

# Minimum temperature difference (°C) considered a real change.
TEMP_TOLERANCE = 0.05
