# Gungor Customizations

Home-grown Home Assistant integration (domain `gungors`): small platforms that patch gaps in
otherwise-good integrations. Installed through HACS as a custom repository (category
**Integration**). Platforms are configured in YAML (`climate: - platform: gungors`,
`cover: - platform: gungors`); the YAML lives in the `harepo` repository (`packages/heating.yaml`,
`packages/covers.yaml`).

| File | What it does |
|---|---|
| `climate.py` | Sync thermostat kept in sync with a physical TRV, PID heater mapped onto the valve opening |
| `cover.py`, `window_guard.py` | Window-guarded covers: remote commands are held while the window is open |
| `timed_curtain.py` | Cover for Tuya curtain motors that only report their position at the end of a move |

Services: `gungors.set_pid_gain`, `gungors.set_pid_mode`, `gungors.set_preset_temp`,
`gungors.clear_integral`, `gungors.reload` (reloads the YAML of all platforms; Python changes
still need a restart).

## Releasing

Bump `version` in `custom_components/gungors/manifest.json`, commit, publish a GitHub release
(`vX.Y.Z`), update it in HACS and restart Home Assistant.
