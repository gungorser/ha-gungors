# ha-integrations (gungors)

Home-grown Home Assistant integration (domain `gungors`): small platforms that patch gaps in
otherwise-good integrations. Installed through HACS as a custom repository (category
**Integration**). Platforms are configured in YAML (`climate: - platform: gungors`,
`cover: - platform: gungors`); the YAML lives in the `ha-configs` repository (`packages/heating.yaml`,
`packages/covers.yaml`).

| File | What it does |
|---|---|
| `climate.py` | Sync thermostat kept in sync with a physical TRV, PID heater mapped onto the valve opening |
| `cover.py`, `window_guard.py` | Window-guarded covers: remote commands are held while the window is open |
| `timed_curtain.py` | Cover for Tuya curtain motors that only report their position at the end of a move |

Services: `gungors.set_pid_gain`, `gungors.set_pid_mode`, `gungors.set_preset_temp`,
`gungors.clear_integral`, `gungors.reload` (reloads the YAML of all platforms; Python changes
still need a restart).

Each module's docstring is its full reference (behaviour, options, attributes, why it exists):
read the README, then only the docstring of the module you touch.

## Wrappers drive hidden originals

Every gungors entity drives an original device entity, which is hidden in Home Assistant:
dashboards and automations use the wrapper (`cover.sercan_cover`, `climate.<room>_thermostat`),
never the original (`cover.sercan_blind`, `climate.<room>_climate`). The wrapper-to-original
pairs are the YAML in ha-configs `packages/covers.yaml` and `packages/heating.yaml`.

## Related repositories

- **ha-configs**: the YAML that configures these platforms (`packages/covers.yaml`,
  `packages/heating.yaml`) and the pushbutton blueprint that fires `gungors_physical_cover`.
- **ha-floorplan / ha-dashboards**: the 3D floor view shows the wrapper entities.

## Releasing

Bump `version` in `custom_components/gungors/manifest.json`, commit, publish a GitHub release
(`vX.Y.Z`), update it in HACS and restart Home Assistant.
