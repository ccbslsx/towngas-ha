"""The Towngas (港华燃气) integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import TokenStore, TownGasApiClient, TownGasApiError, TownGasAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    CONF_REFRESH_TOKEN,
    CONF_SUBSCRIPTIONS,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
    OAUTH_TOKEN_PATH,
    OPT_TOKEN_REFRESH_INTERVAL,
    SERVICE_FORCE_REFRESH,
    VERSION,
)
from .coordinator import TownGasCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Towngas from a config entry."""
    _LOGGER.info(
        "Towngas 港华燃气集成启动 v%s | 刷新端点 %s%s | 是否持有 refresh_token: %s",
        VERSION,
        entry.data[CONF_BASE_URL].rstrip("/"),
        OAUTH_TOKEN_PATH,
        bool(entry.data.get(CONF_REFRESH_TOKEN)),
    )
    session = async_get_clientsession(hass)
    client = TownGasApiClient(
        session,
        entry.data[CONF_BASE_URL],
        TokenStore(
            entry.data[CONF_ACCESS_TOKEN],
            entry.data.get(CONF_REFRESH_TOKEN),
            entry.data.get(CONF_TOKEN_EXPIRES_AT, 0) or 0,
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

    # 注册调试/手动服务（仅注册一次，跨所有 entry 共用）。
    # 用途：手动触发一次真实 token 刷新，用于验证「过期自动刷新」机制是否工作，
    #       或在不想等自动周期时立即续期。刷新成功后 token 传感器状态会同步更新。
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_REFRESH):

        async def _handle_force_refresh(call: ServiceCall) -> dict[str, Any]:
            entry_id = call.data.get("entry_id")
            out: dict[str, Any] = {"results": []}
            for e in hass.config_entries.async_entries(DOMAIN):
                if entry_id and e.entry_id != entry_id:
                    continue
                coordinator = hass.data.get(DOMAIN, {}).get(e.entry_id)
                if coordinator is None:
                    continue
                before = coordinator.client.tokens.expires_at
                ok = await coordinator.client.async_try_refresh_token()
                has_refresh = bool(coordinator.client.tokens.refresh_token)
                if ok:
                    coordinator._persist_tokens()
                    coordinator.async_write_ha_state()
                    after = coordinator.client.tokens.expires_at
                    _LOGGER.info(
                        "强制刷新成功 entry=%s 旧expires_at=%.0f 新expires_at=%.0f",
                        e.title or e.entry_id, before, after,
                    )
                    out["results"].append(
                        {
                            "entry": e.title or e.entry_id,
                            "refreshed": True,
                            "has_refresh_token": has_refresh,
                            "old_expires_at": before,
                            "new_expires_at": after,
                        }
                    )
                else:
                    reason = (
                        coordinator.client.tokens.last_refresh_error
                        or "未知原因"
                    )
                    _LOGGER.warning(
                        "强制刷新失败 entry=%s 原因：%s",
                        e.title or e.entry_id, reason,
                    )
                    # 即使失败也刷新传感器，让「刷新状态」实体显示失败原因
                    coordinator.async_write_ha_state()
                    out["results"].append(
                        {
                            "entry": e.title or e.entry_id,
                            "refreshed": False,
                            "has_refresh_token": has_refresh,
                            "error": reason,
                        }
                    )
            return out

        hass.services.async_register(
            DOMAIN, SERVICE_FORCE_REFRESH, _handle_force_refresh
        )

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
