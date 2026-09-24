"""The gungors integration.

A home-grown collection of small platforms that patch gaps in otherwise-good
HA integrations. Platforms are configured directly in YAML
(`climate: - platform: gungors`, `cover: - platform: gungors`).

The `gungors.reload` service reloads the YAML of all platforms below without
a Home Assistant restart (Python code changes still need a restart).
"""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

PLATFORMS = [Platform.CLIMATE, Platform.COVER]

__all__ = ["DOMAIN", "PLATFORMS"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (platforms are configured via YAML)."""
    return True
