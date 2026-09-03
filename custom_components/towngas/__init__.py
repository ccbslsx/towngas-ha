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
    OPT_TOKEN_REFRESH_INTERVAL,
    SERVICE_DUMP_RAW,
    SERVICE_FORCE_REFRESH,
    VERSION,
    WECHAT_HOST,
    WECHAT_OAUTH_PATH,
)
from .coordinator import TownGasCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Towngas from a config entry."""
    # v1.5.0 起鉴权/数据统一走微信中央网关；忽略旧版可能残留的营业厅 base_url。
    api_base = f"https://{WECHAT_HOST}"
    _LOGGER.info(
        "Towngas 港华燃气集成启动 v%s | 微信中央网关 %s%s | 是否持有 refresh_token: %s",
        VERSION,
        api_base.rstrip("/"),
        WECHAT_OAUTH_PATH,
        bool(entry.data.get(CONF_REFRESH_TOKEN)),
    )
    session = async_get_clientsession(hass)
    client = TownGasApiClient(
        session,
        api_base,
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

    # 启动即做一次 token 健康检查：若 token 在重启时已近过期，立即刷新，
    # 避免等待一个完整 interval 期间 token 过期而直接弹 reauth（参考杭州版
    # 首刷提前到「剩余寿命-60s」的思路）。后台执行，不阻塞 setup。
    hass.async_create_task(coordinator.async_token_health_check())

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

    # 诊断服务：导出各接口原始返回，便于在不暴露 authCode 的情况下校准字段
    # 名与金额单位。用户在 Developer Tools → 服务 调用 `towngas.dump_raw`，
    # 把返回结果贴回即可，无需再把 authCode 交给我。
    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_RAW):

        async def _handle_dump_raw(call: ServiceCall) -> dict[str, Any]:
            entry_id = call.data.get("entry_id")
            out: dict[str, Any] = {}
            for e in hass.config_entries.async_entries(DOMAIN):
                if entry_id and e.entry_id != entry_id:
                    continue
                coordinator = hass.data.get(DOMAIN, {}).get(e.entry_id)
                if coordinator is None:
                    continue
                for sub in coordinator.subscriptions:
                    label = f"{e.title or e.entry_id}/{sub.get('subs_code') or sub.get('subs_id')}"
                    try:
                        out[label] = await coordinator.client.async_dump_raw(sub)
                    except Exception as err:  # noqa: BLE001
                        out[label] = f"ERR {err}"
            return out

        hass.services.async_register(DOMAIN, SERVICE_DUMP_RAW, _handle_dump_raw)

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
