"""Cover platform for the gungors integration.

Cover types (`type:`):
- window_guard: blind that refuses remote movement while a window is open.
  `window` is optional; without it the wrapper only adds opening/closing
  tracking to a blind that reports position alone.
- timed_curtain: curtain motor that only reports position at the end of a
  move; position/direction are estimated in between.
"""
from __future__ import annotations

import logging

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
    CONF_INVERT,
    CONF_START_TIMEOUT,
    CONF_STOP_SILENCE,
    CONF_TYPE,
    CONF_WINDOW,
    CONF_Z2M_BASE_TOPIC,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_SILENCE,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    TYPE_TIMED_CURTAIN,
    TYPE_WINDOW_GUARD,
)
from . import PLATFORMS
from .timed_curtain import TimedCurtainCover
from .window_guard import WindowGuardedCover

_LOGGER = logging.getLogger(__name__)

# One schema for all cover types; type-specific requirements are checked in
# async_setup_platform.
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In([TYPE_WINDOW_GUARD, TYPE_TIMED_CURTAIN]),
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_COVER): cv.entity_id,
        # window_guard
        vol.Optional(CONF_WINDOW): cv.entity_id,
        vol.Optional(CONF_START_TIMEOUT, default=DEFAULT_START_TIMEOUT): vol.Coerce(float),
        vol.Optional(CONF_STOP_SILENCE, default=DEFAULT_STOP_SILENCE): vol.Coerce(float),
        # timed_curtain
        vol.Optional(CONF_INVERT, default=False): cv.boolean,
        vol.Optional(CONF_Z2M_BASE_TOPIC, default=DEFAULT_Z2M_BASE_TOPIC): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up gungors cover entities from YAML."""
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    cover_type = config[CONF_TYPE]

    unique_id = config.get(CONF_UNIQUE_ID) or config[CONF_NAME]

    if cover_type == TYPE_TIMED_CURTAIN:
        async_add_entities(
            [
                TimedCurtainCover(
                    name=config[CONF_NAME],
                    unique_id=unique_id,
                    raw_cover_entity_id=config[CONF_COVER],
                    invert=config[CONF_INVERT],
                    z2m_base_topic=config[CONF_Z2M_BASE_TOPIC],
                )
            ]
        )
        return

    if cover_type == TYPE_WINDOW_GUARD:
        async_add_entities(
            [
                WindowGuardedCover(
                    name=config[CONF_NAME],
                    unique_id=unique_id,
                    raw_cover_entity_id=config[CONF_COVER],
                    window_entity_id=config.get(CONF_WINDOW),
                    start_timeout=config[CONF_START_TIMEOUT],
                    stop_silence=config[CONF_STOP_SILENCE],
                )
            ]
        )
