"""Cover platform for the gungors integration.

Currently only one cover "type" exists: window_guard. The `type` key is
kept so more cover behaviors can be added later without a new platform.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.cover import PLATFORM_SCHEMA
from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_COVER,
    CONF_START_TIMEOUT,
    CONF_STOP_SILENCE,
    CONF_TYPE,
    CONF_WINDOW,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_SILENCE,
    DOMAIN,
    TYPE_WINDOW_GUARD,
)
from . import PLATFORMS
from .window_guard import WindowGuardedCover

WINDOW_GUARD_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In([TYPE_WINDOW_GUARD]),
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_COVER): cv.entity_id,
        vol.Required(CONF_WINDOW): cv.entity_id,
        vol.Optional(CONF_START_TIMEOUT, default=DEFAULT_START_TIMEOUT): vol.Coerce(float),
        vol.Optional(CONF_STOP_SILENCE, default=DEFAULT_STOP_SILENCE): vol.Coerce(float),
    }
)

PLATFORM_SCHEMA = WINDOW_GUARD_SCHEMA


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up gungors cover entities from YAML."""
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    cover_type = config[CONF_TYPE]

    if cover_type == TYPE_WINDOW_GUARD:
        unique_id = config.get(CONF_UNIQUE_ID) or config[CONF_NAME]
        async_add_entities(
            [
                WindowGuardedCover(
                    name=config[CONF_NAME],
                    unique_id=unique_id,
                    raw_cover_entity_id=config[CONF_COVER],
                    window_entity_id=config[CONF_WINDOW],
                    start_timeout=config[CONF_START_TIMEOUT],
                    stop_silence=config[CONF_STOP_SILENCE],
                )
            ]
        )
