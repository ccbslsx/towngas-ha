"""The Towngas (港华燃气) integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import TokenStore, TownGasApiClient, TownGasApiError, TownGasAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    CONF_REFRESH_TOKEN,
    CONF_SUBSCRIPTIONS,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
    OPT_TOKEN_REFRESH_INTERVAL,
)
from .coordinator import TownGasCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Towngas from a config entry."""
    session = async_get_clientsession(hass)
    client = TownGasApiClient(
        session,
        entry.data[CONF_BASE_URL],
        TokenStore(
            entry.data[CONF_ACCESS_TOKEN],
            entry.data.get(CONF_REFRESH_TOKEN),
        ),
    )
    coordinator = TownGasCoordinator(
        hass,
        client,
        entry,
        entry.data[CONF_SUBSCRIPTIONS],
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        # HA will automatically start the reauth flow.
        raise
    except ConfigEntryNotReady:
        raise
    except TownGasAuthError as err:
        raise ConfigEntryAuthFailed from err
    except TownGasApiError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err

    # 独立的 Token 健康检查定时器（不受维护窗口影响）。
    token_interval = entry.options.get(
        OPT_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL
    )
    if token_interval and token_interval > 0:
        entry.async_on_unload(
            async_track_time_interval(
                hass,
                coordinator.async_token_health_check,
                timedelta(seconds=token_interval),
            )
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its data or options change."""
    await hass.config_entries.async_reload(entry.entry_id)
