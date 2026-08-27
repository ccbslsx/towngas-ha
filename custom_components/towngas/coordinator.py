"""Data coordinator for the Towngas (港华燃气) integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TownGasApiClient, TownGasApiError, TownGasAuthError
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
    MAINTENANCE_END_HOUR,
    MAINTENANCE_END_MINUTE,
    MAINTENANCE_START_HOUR,
    MAINTENANCE_START_MINUTE,
    OPT_SCAN_INTERVAL,
    OPT_TOKEN_REFRESH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


class TownGasCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetch bill data for every selected subscription."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TownGasApiClient,
        entry: ConfigEntry,
        subscriptions: list[dict[str, Any]],
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.entry = entry
        self.subscriptions = subscriptions
        interval_seconds = entry.options.get(
            OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval_seconds),
        )

    async def _fetch_sub(
        self, subs_code: str, org_code: str
    ) -> dict[str, Any]:
        """Fetch all data for one subscription; tolerate partial failures."""
        result: dict[str, Any] = {
            "subs_code": subs_code,
            "org_code": org_code,
            "sub_info": {},
            "bills": [],
            "balance": None,
            "arrears": None,
            "unpaid_count": 0,
        }

        # 1. Bill history (contains readings / usage / amounts)
        try:
            result["bills"] = await self.client.async_get_bills(
                subs_code, org_code
            )
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取账单列表失败 %s: %s", subs_code, err)

        # 2. Account balance
        try:
            result["balance"] = await self.client.async_get_balance(
                subs_code, org_code
            )
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取余额失败 %s: %s", subs_code, err)

        # 3. Arrears (欠费) - preferred source
        try:
            unpaid = await self.client.async_get_unpaid(subs_code, org_code)
            raw_arrears = _to_float(unpaid.get("totalUnpaidFee"))
            # 无欠费时接口返回空字符串 "", 视为 0; 金额单位为「分」需 ÷100
            arrears = (raw_arrears if raw_arrears is not None else 0.0) / 100.0
            arrears = round(arrears, 2)
            unpaid_datas = unpaid.get("datas") or []
            if not unpaid_datas:
                # 兜底: 从账单列表里找未结清的
                unpaid_datas = [
                    b for b in result["bills"] if _to_float(b.get("totalUnpaidFee"))
                ]
            result["arrears"] = arrears
            result["unpaid_count"] = len(unpaid_datas)
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取欠费信息失败 %s: %s", subs_code, err)

        # 4. Subscription info (户主/户址) - nice to have
        try:
            result["sub_info"] = await self.client.async_get_sub_info(
                subs_code, org_code
            )
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取户信息失败 %s: %s", subs_code, err)

        return result

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Update data for all subscriptions.

        During the daily maintenance window (23:30-00:30 CST) data requests
        are skipped and the last good reading is returned unchanged.
        """
        if self.data is not None and self._in_maintenance(dt_util.now()):
            _LOGGER.debug(
                "港华燃气系统维护窗口(23:30-00:30)，跳过本次数据请求，"
                "传感器保持上次读数"
            )
            return self.data

        results = await asyncio.gather(
            *(
                self._fetch_sub(sub["subs_code"], sub["org_code"])
                for sub in self.subscriptions
            ),
            return_exceptions=True,
        )

        for sub, res in zip(self.subscriptions, results):
            if isinstance(res, TownGasAuthError):
                if await self.client.async_try_refresh_token():
                    self._persist_tokens()
                    return await self._async_update_data()
                raise ConfigEntryAuthFailed from res
            if isinstance(res, BaseException):
                raise UpdateFailed(f"更新港华燃气数据失败: {res}")

        data: dict[str, dict[str, Any]] = {}
        for sub, res in zip(self.subscriptions, results):
            data[f"{sub['org_code']}_{sub['subs_code']}"] = res
        return data

    @staticmethod
    def _in_maintenance(now: datetime) -> bool:
        """Return True if *now* (HA local time) is inside 23:30-00:30 CST."""
        cur = now.hour * 60 + now.minute
        start = MAINTENANCE_START_HOUR * 60 + MAINTENANCE_START_MINUTE
        end = MAINTENANCE_END_HOUR * 60 + MAINTENANCE_END_MINUTE
        if start <= end:
            return start <= cur <= end
        # 跨午夜窗口
        return cur >= start or cur <= end

    async def async_token_health_check(self, now: datetime | None = None) -> None:
        """Periodically verify the token is still valid.

        Runs on its own interval (token_refresh_interval) and is NOT skipped
        during the maintenance window. Detects expiry early and triggers the
        reauth flow so the user is prompted for a fresh token.
        """
        if not self.client.tokens.access_token:
            return
        try:
            await self.client.async_validate_token()
        except TownGasAuthError:
            _LOGGER.warning("港华燃气 token 已失效，触发重新认证流程")
            self.hass.config_entries.async_start_reauth(
                self.hass, self.entry.entry_id
            )
        except TownGasApiError as err:
            # 网络抖动：下个周期再试，不触发 reauth
            _LOGGER.debug("Token 健康检查请求失败(可能网络抖动)，跳过: %s", err)

    def _persist_tokens(self) -> None:
        """Write a refreshed access token back into the config entry."""
        new_data = dict(self.entry.data)
        new_data["access_token"] = self.client.tokens.access_token
        if self.client.tokens.refresh_token:
            new_data["refresh_token"] = self.client.tokens.refresh_token
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
