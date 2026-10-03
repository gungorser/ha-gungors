# Gungor Customizations

Home-grown Home Assistant integration (domain `gungors`): small platforms that patch gaps in
otherwise-good integrations. Installed through HACS as a custom repository (category
**Integration**). Platforms are configured in YAML (`climate: - platform: gungors`,
`cover: - platform: gungors`); the YAML lives in the `ha-configs` repository (`packages/heating.yaml`,
`packages/covers.yaml`).

## The wrapper rule

Every entity of this integration **drives an original entity** (a Zigbee2MQTT blind, a Tuya curtain
motor, a TRV). The originals are hidden in Home Assistant: dashboards, automations and schedules must
use the wrapper (`cover.<room>_cover`, `climate.<room>_thermostat`), never the raw entity
(`*_blind`, `*_curtain`, `*_climate`).

## Files

```
custom_components/gungors/
  __init__.py       platforms (climate, cover), gungors.reload
  const.py          config keys, defaults, attribute names, the physical-cover event
  cover.py          cover platform: schema, dispatch by `type:` to the two classes below
  window_guard.py   type window_guard
  timed_curtain.py  type timed_curtain
  climate.py        sync thermostat (subclass of smart_thermostat) and its services
  services.yaml     service descriptions
  manifest.json     version (bump on every release)
```

## cover: `type: window_guard` (IKEA blinds over Zigbee2MQTT)

Holds remote commands while the optional `window` contact is open: the target is kept (attribute
`is_sync: false`, the target shown as position) and sent when the window closes. Any move of the raw
blind that the wrapper did not command is physical and always allowed. Unbound push buttons go through
the `pushbutton` blueprint (ha-configs), which fires event `gungors_physical_cover`
`{entity_id, action: open|close}`: those moves ignore the window. The raw blind reports no
opening/closing, so the wrapper derives it from position reports and ignores the Zigbee2MQTT target
echo (a jump > 5 straight to the target).

Options: `cover`, `window` (optional), `start_timeout` (8 s), `stop_silence` (3 s).
Attributes: `is_sync`, `actual_position`.

| wrapper | drives | window |
|---|---|---|
| `cover.sercan_cover` | `cover.sercan_blind` | `binary_sensor.sercan_window_contact` |
| `cover.bebek_cover` | `cover.bebek_blind` | `binary_sensor.bebek_window_contact` |
| `cover.yatak_cover_window` | `cover.yatak_blind_2` | `binary_sensor.yatak_window_contact` |
| `cover.koridor1_cover` | `cover.koridor1_blind` | none (only adds opening/closing) |

`cover.yatak_cover` is a `group` cover in ha-configs: `yatak_cover_window` + `yatak_blind_1` + `yatak_blind_3`.

## cover: `type: timed_curtain` (Tuya curtain motors over Zigbee2MQTT)

The motor reports its position only at the end of a move. The wrapper listens to the Zigbee2MQTT
topic directly and estimates position and direction while moving from learned full-run times per
direction (`open_time`/`close_time`, start 10 s, learned from moves of at least 30 %). `invert: true`
flips open/close and positions (motor "open" = room closed). It ignores the echo and the bogus first
end report after a power loss (`calibrated: false` until a real move).

Options: `cover`, `invert` (false), `z2m_base_topic` (`zigbee2mqtt`).
Attributes: `position_source` (`reported`|`estimated`), `open_time`, `close_time`, `calibrated`.

| wrapper | drives |
|---|---|
| `cover.salon_cover` | `cover.salon_curtain` (invert) |
| `cover.misafir_cover` | `cover.misafir_curtain` (invert) |

## climate: sync thermostat

Subclasses ScratMan's `smart_thermostat` (PID); it must be installed as
`custom_components/smart_thermostat`. Mode and target are synced both ways with the physical TRV
(`input_device`). `heater` given as a dict (`valve_opening`, `valve_closing`, `valve_min`) scales the
PID output 0-100 onto the TRV's valve opening/closing degree numbers. Unavailable while the physical
thermostat is. Leaving OFF restores the target from before OFF; `set_temperature` while OFF switches to
heat.

| wrapper | drives |
|---|---|
| `climate.<room>_thermostat` (sercan, misafir, bebek, melike, banyo, yatak) | `climate.<room>_climate` + `number.<room>_climate_valve_{opening,closing}_degree` |
| `climate.salon_thermostat` | `climate.thermostat_hc1` (EMS-ESP), heater `switch.koridor0_socket`, PWM 15 min |

Temperature sensors: `sensor.<room>_sensor_temperature` (banyo `sensor.banyo_moisture_temperature`,
salon `sensor.thermostat_hc1_currtemp`). The boiler demand (`binary_sensor.kombi_talebi` -> hidden
relay `switch.koridor2_switch`) is in ha-configs, not here.

Services: `gungors.set_pid_gain`, `gungors.set_pid_mode`, `gungors.set_preset_temp`,
`gungors.clear_integral`, `gungors.reload` (reloads the YAML of all platforms; Python changes
still need a restart).

## Changing it

- YAML only (ha-configs): edit, deploy the package, call `gungors.reload`.
- Python: change here, bump `version` in `manifest.json`, commit, publish a GitHub release
  (`vX.Y.Z`), update it in HACS and restart Home Assistant.
- Commit messages and code comments in English.
