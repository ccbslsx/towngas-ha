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
    TOKEN_EXPIRY_BUFFER_SECS,
    TOKEN_PERSIST_IN_PROGRESS,
    TOKEN_REFRESH_FAILURE_THRESHOLD,
    TOKEN_REFRESH_SAFETY_MARGIN_SECS,
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
        # 连续刷新失败次数（抗抖动：达阈值才 reauth，见 async_token_health_check）
        self._consecutive_refresh_failures: int = 0
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval_seconds),
        )

    def _refresh_buffer(self) -> int:
        """主动刷新的提前量（秒）。

        为什么不能只用固定的 60 秒：保活定时器每 ``interval`` 秒才跑一次，
        若提前量小于间隔，检查点可能落在 token **已经过期之后**。
        例：token T 时刻签发（7200s 寿命），定时器在 T+1800/3600/5400/7200 检查，
        提前量 60s → 需到 T+7140 之后才刷，而实际检查点是 T+7200 —— 此时
        token 已死，必须先挨一次鉴权失败才能恢复；若定时器相位再偏一点
        （比如 T+7300 才检查），token 已过期 100 秒。

        所以提前量取「一个完整检查间隔 + 安全余量」，确保任何相位下刷新
        检查点都落在 token 有效期内。
        """
        interval = int(self._token_refresh_interval or 0)
        return max(TOKEN_EXPIRY_BUFFER_SECS, interval + TOKEN_REFRESH_SAFETY_MARGIN_SECS)

    async def _fetch_sub(self, sub: dict[str, Any]) -> dict[str, Any]:
        """Fetch all data for one subscription; tolerate partial failures."""
        subs_id = sub.get("subs_id")
        subs_code = sub.get("subs_code")
        org_code = sub.get("org_code")
        result: dict[str, Any] = {
            "subs_id": subs_id,
            "subs_code": subs_code,
            "org_code": org_code,
            "sub_info": {},
            "reading": {},
            "bills": [],
            "balance": None,
            "arrears": None,
            "unpaid_count": 0,
        }

        # 每次拉取前先保活：确保本户号所有接口调用都使用新鲜 token
        # （参考杭州版 _cbs_get 内 ensure_token() 的思路，避免多户号逐个拉取
        #  中途 token 过期导致个别接口返回 20001）。即便此处刷新失败，下方
        #  各 _get 仍会在遇 20001 时就地刷新重试一次。
        # 用动态提前量，避免「检查点落在 token 过期之后」。
        if self.client.tokens.is_near_expiry(self._refresh_buffer()):
            if await self.client.async_try_refresh_token():
                self._persist_tokens()

        # 1. 本期表数（核心、已验证可用）：preCheck(subsId) 优先，回退 subsCode+orgCode。
        #    这是 v1.5.0 解决「token 过期」之外的主数据，单独容错，失败不影响其它。
        try:
            reading = await self.client.async_get_reading(
                subs_id, subs_code, org_code
            )
            if reading:
                result["reading"] = reading
        except TownGasAuthError:
            raise
        except TownGasApiError as err:
            _LOGGER.warning("获取表数失败 %s: %s", subs_id or subs_code, err)

        # 2. 历史账单 / 余额 / 户信息（best-effort）。
        #    vcc-cbs 的 queryHistoryFee / gasFeeBaseinfo 认 subsId（气户标识），与
        #    preCheck 一致；无 subs_id 时才回退 subsCode+orgCode。字段名与金额
        #    单位已用真实 token 校准（dump_raw 实测）：余额优先用 preCheck 的
        #    savingSum（预付费充值余额，单位元），账单接口作兜底。
        if subs_id or (subs_code and org_code):
            try:
                result["bills"] = await self.client.async_get_bills(
                    subs_id, subs_code, org_code
                )
            except TownGasAuthError:
                raise
            except TownGasApiError as err:
                _LOGGER.warning("获取账单列表失败 %s: %s", subs_code, err)

            try:
                balance_info = await self.client.async_get_balance(
                    subs_id, subs_code, org_code
                )
            except TownGasAuthError:
                raise
            except TownGasApiError as err:
                _LOGGER.warning("获取余额失败 %s: %s", subs_id or subs_code, err)
                balance_info = None

            try:
                result["sub_info"] = await self.client.async_get_sub_info(
                    subs_code, org_code
                )
            except TownGasAuthError:
                raise
            except TownGasApiError as err:
                _LOGGER.warning("获取户信息失败 %s: %s", subs_code, err)

            # 余额：预付费用户的充值余额来自 preCheck.savingSum（最可靠）；
            #    否则用 gasFeeBaseinfo 的 availableBalance（已用 subsId 调用）。
            reading = result.get("reading") or {}
            raw_saving = reading.get("savingSum")
            if raw_saving not in (None, ""):
                result["balance"] = _to_float(raw_saving)
            elif balance_info:
                result["balance"] = balance_info.get("available_balance")
            else:
                result["balance"] = None
            result["balance_info"] = balance_info
        else:
            result["balance"] = None
            result["balance_info"] = None

        # 3. 欠费：预付费户不会欠费（余额即充值，不生成账单欠款），直接记 0；
        #    后付费户从账单列表推导（totalUnpaidFee 之和），并叠加接口返回的
        #    gasFeeBaseinfo.feePayable（已出账未缴金额）。
        charge_type = (result.get("reading") or {}).get("chargeType")
        balance_info = result.get("balance_info") or {}
        if charge_type == "prepay":
            result["arrears"] = 0.0
            result["unpaid_count"] = 0
        else:
            bills = result.get("bills") or []
            unpaid_datas = [b for b in bills if _to_float(b.get("totalUnpaidFee"))]
            raw_arrears = sum(
                (_to_float(b.get("totalUnpaidFee")) or 0.0) for b in unpaid_datas
            )
            fp = _to_float(balance_info.get("fee_payable"))
            if fp:
                raw_arrears = max(raw_arrears, fp)
            result["arrears"] = round(raw_arrears, 2)
            result["unpaid_count"] = len(unpaid_datas)

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
        #    用动态提前量（一个检查间隔 + 余量），保证刷新点落在有效期内。
        if self.client.tokens.expires_at and self.client.tokens.is_near_expiry(
            self._refresh_buffer()
        ):
            if await self.client.async_try_refresh_token():
                self._persist_tokens()

        results = await asyncio.gather(
            *(
                self._fetch_sub(sub)
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
            key = sub.get("subs_code") or sub.get("subs_id") or "default"
            data[key] = res
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

        # 1. 主动刷新：已知过期时间且临近过期（用动态提前量，见 _refresh_buffer）
        if self.client.tokens.expires_at and self.client.tokens.is_near_expiry(
            self._refresh_buffer()
        ):
            if await self.client.async_try_refresh_token():
                self._persist_tokens()
                self.last_token_refresh = time.time()
                self.last_token_refresh_ok = True
                self._consecutive_refresh_failures = 0
                _LOGGER.info("港华燃气 token 主动刷新成功")
                return
            # 临近过期但刷新失败 → 落到下面的校验分支决定是否需要 reauth

        # 2. 校验 → 失败则尝试刷新 → 连续失败达阈值才 reauth
        try:
            await self.client.async_validate_token()
        except TownGasAuthError:
            if await self.client.async_try_refresh_token():
                self._persist_tokens()
                self.last_token_refresh = time.time()
                self.last_token_refresh_ok = True
                self._consecutive_refresh_failures = 0
                _LOGGER.info("港华燃气 token 过期后刷新成功")
                return
            self.last_token_refresh_ok = False
            self._consecutive_refresh_failures += 1
            reason = self.client.tokens.last_refresh_error or "未知原因"
            # 抗抖动：单次失败绝不立刻 reauth。网络抖动、服务端临时错误、
            # 维护窗口都可能导致一次失败，而下一个周期往往自愈。
            # 只有连续失败达到阈值（默认 3 次 ≈ 1.5 小时）才判定 token 真的废了。
            if self._consecutive_refresh_failures < TOKEN_REFRESH_FAILURE_THRESHOLD:
                _LOGGER.warning(
                    "港华燃气 token 刷新失败（第 %s/%s 次，暂不 reauth，"
                    "下个周期自动重试）：%s",
                    self._consecutive_refresh_failures,
                    TOKEN_REFRESH_FAILURE_THRESHOLD,
                    reason,
                )
                return
            _LOGGER.error(
                "港华燃气 token 连续 %s 次刷新失败，判定已失效，触发重新认证：%s",
                self._consecutive_refresh_failures,
                reason,
            )
            self._consecutive_refresh_failures = 0
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
        else:
            # 校验通过（token 仍然有效）→ 重置连续失败计数，
            # 避免历史抖动累积把集成推向 reauth。
            if self._consecutive_refresh_failures:
                _LOGGER.info(
                    "港华燃气 token 恢复正常（此前连续失败 %s 次已清零）",
                    self._consecutive_refresh_failures,
                )
                self._consecutive_refresh_failures = 0

    def _persist_tokens(self) -> None:
        """Write refreshed tokens + expiry back into the config entry.

        v1.5.2 **关键修复**：``async_update_entry`` 会触发 entry 的 update
        listener，而 listener 原本无条件 ``async_reload()`` → 每刷新一次 token
        就重载一次集成（实体周期性 unavailable，且某次重载失败会打死集成）。
        这里在写回前登记 entry_id，listener 命中后跳过 reload。
        """
        new_data = dict(self.entry.data)
        new_data[CONF_ACCESS_TOKEN] = self.client.tokens.access_token
        if self.client.tokens.refresh_token:
            new_data[CONF_REFRESH_TOKEN] = self.client.tokens.refresh_token
        new_data[CONF_TOKEN_EXPIRES_AT] = self.client.tokens.expires_at
        TOKEN_PERSIST_IN_PROGRESS.add(self.entry.entry_id)
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        # 兜底清理：listener 会在下一个事件循环迭代消费掉标记。
        # 若因集成正在卸载等原因 listener 未执行，10 秒后强制清理，
        # 避免标记残留导致后续真正的 options 变更被误跳过 reload。
        # 注意：不能用 try/finally 立即清理——async_update_entry 内部是用
        # async_create_task 调度 listener 的，同步 finally 会在 listener
        # 执行之前就清掉标记，抑制就失效了。
        self.hass.loop.call_later(
            10, TOKEN_PERSIST_IN_PROGRESS.discard, self.entry.entry_id
        )

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
