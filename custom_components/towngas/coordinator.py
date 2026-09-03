"""Data coordinator for the Towngas (港华燃气) integration."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TownGasApiClient, TownGasApiError, TownGasAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
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
        self._scan_interval = interval_seconds
        self._token_refresh_interval = entry.options.get(
            OPT_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL
        )
        # 供传感器展示「下次刷新倒计时」等可观测信息
        self.last_token_refresh: float | None = None
        self.last_token_refresh_ok: bool | None = None
        self._last_update_ts: float | None = None
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

        # 每次拉取前先保活：确保本户号所有接口调用都使用新鲜 token
        # （参考杭州版 _cbs_get 内 ensure_token() 的思路，避免多户号逐个拉取
        #  中途 token 过期导致个别接口返回 20001）。即便此处刷新失败，下方
        #  各 _get 仍会在遇 20001 时就地刷新重试一次。
        await self.client.async_refresh_if_near_expiry()

        # 1. Bill history (contains readings / usage / amounts)
        try:
            result["bills"] = await self.client.async_get_bills(
                subs_code, org_code
            )
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取账单列表失败 %s: %s", subs_code, err)

        # 1b. 本期表数兜底：vcc-cbs 的 queryHistoryFee 可能不含 currReading，
        #     用 charge/preCheck 单独取一次并补进最新一条账单（传感器据此出表数）。
        bills = result.get("bills") or []
        if bills and not bills[0].get("currReading"):
            try:
                reading = await self.client.async_get_reading(subs_code, org_code)
                if reading:
                    bills[0].setdefault("currReading", reading.get("currReading"))
                    bills[0].setdefault("lastReading", reading.get("lastReading"))
            except TownGasApiError as err:
                _LOGGER.debug("获取表数失败 %s: %s", subs_code, err)

        # 2. Account balance
        try:
            result["balance"] = await self.client.async_get_balance(
                subs_code, org_code
            )
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取余额失败 %s: %s", subs_code, err)

        # 3. Arrears (欠费) — v1.5.0 改为直接从账单列表推导
        #    （历史账单里每条都带 totalUnpaidFee，累计未结清即为欠费）。
        #    单位待探测确认：营业厅为「分」需 ÷100；vcc-cbs 单位未知，先原值透传，
        #    探测锁定字段后统一校准。
        unpaid_datas = [
            b for b in bills if _to_float(b.get("totalUnpaidFee"))
        ]
        raw_arrears = sum(
            (_to_float(b.get("totalUnpaidFee")) or 0.0) for b in unpaid_datas
        )
        result["arrears"] = round(raw_arrears, 2)
        result["unpaid_count"] = len(unpaid_datas)

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

        # 0. 主动续期：已知过期时间且临近过期时，先刷新再拉数据，
        #    避免把请求打在已经过期的 token 上（借鉴杭州项目的 ensure_token 思路）。
        if await self.client.async_refresh_if_near_expiry():
            self._persist_tokens()

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
        self._last_update_ts = time.time()
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
        """Periodically keep the token alive.

        Runs on its own interval (token_refresh_interval) and is NOT skipped
        during the maintenance window. Order of operations:

        1. If the access token is known to be near expiry, proactively refresh
           it (silent keepalive — never prompts the user).
        2. Otherwise validate the token. On an auth error, try to refresh;
           only if that also fails do we fall back to the reauth flow.
        """
        if not self.client.tokens.access_token:
            return

        # 1. 主动刷新：已知过期时间且临近过期
        if self.client.tokens.is_near_expiry():
            if await self.client.async_try_refresh_token():
                self._persist_tokens()
                self.last_token_refresh = time.time()
                self.last_token_refresh_ok = True
                _LOGGER.info("港华燃气 token 主动刷新成功")
                return
            # 临近过期但刷新失败 → 落到下面的校验分支决定是否需要 reauth

        # 2. 校验 → 失败则尝试刷新 → 仍失败才 reauth
        try:
            await self.client.async_validate_token()
        except TownGasAuthError:
            if await self.client.async_try_refresh_token():
                self._persist_tokens()
                self.last_token_refresh = time.time()
                self.last_token_refresh_ok = True
                _LOGGER.info("港华燃气 token 过期后刷新成功")
                return
            _LOGGER.warning("港华燃气 token 已失效且刷新失败，触发重新认证流程")
            self.last_token_refresh_ok = False
            try:
                # HA 2024.2+ 的推荐写法：直接传 ConfigEntry，由框架创建 reauth 流。
                # （旧版 async_start_reauth(flow_id) 已移除，调用会抛 AttributeError。）
                self.hass.config_entries.async_reauth_entry(self.entry)
            except Exception as err:  # noqa: BLE001
                # 即使触发失败也不能让健康检查后台任务抛出未捕获异常
                _LOGGER.error("触发重新认证流程失败: %s", err)
        except TownGasApiError as err:
            # 网络抖动：下个周期再试，不触发 reauth
            _LOGGER.debug("Token 健康检查请求失败(可能网络抖动)，跳过: %s", err)

    def _persist_tokens(self) -> None:
        """Write refreshed tokens + expiry back into the config entry."""
        new_data = dict(self.entry.data)
        new_data[CONF_ACCESS_TOKEN] = self.client.tokens.access_token
        if self.client.tokens.refresh_token:
            new_data[CONF_REFRESH_TOKEN] = self.client.tokens.refresh_token
        new_data[CONF_TOKEN_EXPIRES_AT] = self.client.tokens.expires_at
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # 可观测性：下次刷新时间（供传感器展示「倒计时」）
    # ------------------------------------------------------------------
    def next_token_refresh_at(self) -> datetime | None:
        """下次 token 健康检查（保活）的大致时间（UTC）。

        基于最近一次刷新成功时间 + 间隔推算；若从未成功刷新则回退为
        启动时间 + 间隔。仅用于展示，不影响实际调度。
        """
        if not self._token_refresh_interval:
            return None
        base = self.last_token_refresh or time.time()
        return datetime.fromtimestamp(
            base + self._token_refresh_interval, tz=timezone.utc
        )

    def next_data_refresh_at(self) -> datetime | None:
        """下次数据刷新的大致时间（UTC），基于最近一次成功更新 + 扫描间隔。"""
        if self._last_update_ts is None:
            return None
        return datetime.fromtimestamp(
            self._last_update_ts + self._scan_interval, tz=timezone.utc
        )
