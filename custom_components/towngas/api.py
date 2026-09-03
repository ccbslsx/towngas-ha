"""API client for the Towngas (港华燃气) WeChat-central VCC gateway.

Reverse engineered from the WeChat H5 frontend (weixin.towngasvcc.com/h5-gas)
and cross-checked against the Hangzhou reference integration
(palafin02back/hztowngas).

v1.5.0 — switched from the 网上营业厅 openapi gateway to the WeChat-central
VCC gateway to fix the "token expires every few days" problem:

* Auth uses the WeChat OAuth (``/vcc-oauth/oauth/authorize2/...``):
    - login link: ``union?clientid=...&redirectUri=...``  (open in WeChat)
    - exchange:    ``POST accessToekn?authCode=<code>``      → access+refresh
    - refresh:     ``POST refreshToken?timestamp=&refreshToken=&sign=``
      where ``sign = MD5(sorted("k{v}") + SALT).upper()`` with
      ``SALT = "hbasesoft.com-prod"``.
  access_token lives 7200s; refresh_token survives days (user-proven 1+ week).
* Business calls hit ``{host}/nv1/vcc-cbs/<path>`` (GET), authenticated with
  ``Authorization: Bearer <access_token>`` plus the same ``timestamp``+``sign``
  signature. A 401 triggers one refresh+retry (mirrors the Hangzhou 401 fix).

The central gateway does NOT share tokens with the 营业厅 gateway, so both the
auth layer and the data layer move here together.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from .const import (
    DEFAULT_TOKEN_EXPIRES_IN,
    TOKEN_EXPIRY_BUFFER_SECS,
    USER_AGENT,
    WECHAT_API_PATH,
    WECHAT_APPID,
    WECHAT_CLIENT_ID,
    WECHAT_HOST,
    WECHAT_OAUTH_PATH,
    WECHAT_REDIRECT_URI,
    WECHAT_SIGN_SALT,
)

_LOGGER = logging.getLogger(__name__)

# resultCode values that mean "token invalid / expired" (carried in JSON body).
AUTH_ERROR_CODES = {"20001", "40058"}


# ---------------------------------------------------------------------------
# Signature helpers (WeChat-central gateway)
# ---------------------------------------------------------------------------
def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sign(params: dict[str, Any]) -> str:
    """MD5 of sorted key+value pairs concatenated, plus SALT, uppercased."""
    keys = sorted(
        k for k, v in params.items()
        if k != "sign" and v is not None and v != ""
    )
    raw = "".join(f"{k}{params[k]}" for k in keys)
    return _md5(raw + WECHAT_SIGN_SALT).upper()


def _to_float(value: Any) -> float | None:
    """宽松转 float：None/空串/非法值返回 None。"""
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class TownGasAuthError(Exception):
    """Raised when the access token is invalid or expired."""


class TownGasApiError(Exception):
    """Raised for any other API error."""


class TokenStore:
    """Mutable holder so the client can use refreshed tokens."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: float = 0.0,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        # epoch 秒；0 = 未知（退化为被动刷新）
        self.expires_at = float(expires_at or 0.0)
        # 最近一次刷新失败的原因（成功时置 None）。
        self.last_refresh_error: str | None = None

    def is_near_expiry(self, buffer: int = TOKEN_EXPIRY_BUFFER_SECS) -> bool:
        """True 当已知过期时间且已接近过期（提前 buffer 秒）。"""
        if not self.expires_at:
            return False
        return (self.expires_at - buffer) <= time.time()


class TownGasApiClient:
    """Async client around the WeChat-central VCC gateway."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token_store: TokenStore,
    ) -> None:
        self._session = session
        self._base_url = (base_url or f"https://{WECHAT_HOST}").rstrip("/")
        self.tokens = token_store

    # ------------------------------------------------------------------
    # OAuth helpers
    # ------------------------------------------------------------------
    def get_oauth_url(self) -> str:
        """Return the WeChat OAuth login URL (open in WeChat to scan/log in)."""
        ru = quote(WECHAT_REDIRECT_URI, safe="")
        return (
            f"{self._base_url}{WECHAT_OAUTH_PATH}/oauth/authorize2/union"
            f"?clientid={WECHAT_CLIENT_ID}&redirectUri={ru}"
        )

    async def async_exchange_auth_code(self, auth_code: str) -> bool:
        """Exchange a WeChat login ``authCode`` for access+refresh tokens.

        Returns True and updates the token store on success. On failure sets
        ``last_refresh_error`` and returns False (caller should show reauth).
        """
        if not auth_code:
            self.tokens.last_refresh_error = "未提供 authCode（微信登录后回跳地址里的 code 参数）"
            return False
        url = (
            f"{self._base_url}{WECHAT_OAUTH_PATH}/oauth/authorize2/accessToekn"
            f"?authCode={quote(auth_code, safe='')}"
        )
        try:
            async with self._session.post(
                url, headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            self.tokens.last_refresh_error = f"换发端点返回 HTTP {err.status}"
            return False
        except (aiohttp.ClientError, json.JSONDecodeError, TimeoutError, asyncio.TimeoutError) as err:
            self.tokens.last_refresh_error = f"网络错误：{err}"
            return False

        new_access = data.get("access_token") if isinstance(data, dict) else None
        if not new_access:
            safe = data if isinstance(data, dict) else {}
            rc = safe.get("resultCode") or safe.get("result_code")
            msg = safe.get("resultMsg") or safe.get("result_msg") or ""
            self.tokens.last_refresh_error = (
                f"服务端拒绝 resultCode={rc} {msg}".strip() or "响应异常"
            )
            return False

        self.tokens.access_token = new_access
        if data.get("refresh_token"):
            self.tokens.refresh_token = data["refresh_token"]
        self.tokens.expires_at = time.time() + int(
            data.get("expires_in") or DEFAULT_TOKEN_EXPIRES_IN
        )
        self.tokens.last_refresh_error = None
        _LOGGER.info("微信 OAuth 换发成功，有效期 %s 秒", data.get("expires_in"))
        return True

    async def async_try_refresh_token(self) -> bool:
        """Use refresh_token to obtain a fresh access_token (WeChat OAuth).

        Returns True and updates the token store on success.
        """
        refresh_token = self.tokens.refresh_token
        if not refresh_token:
            self.tokens.last_refresh_error = "未保存 refresh_token（粘贴内容里没有 refresh_token 字段）"
            return False

        ts = int(time.time() * 1000)
        sign = _sign({"timestamp": ts, "refreshToken": refresh_token})
        url = (
            f"{self._base_url}{WECHAT_OAUTH_PATH}/oauth/authorize2/refreshToken"
            f"?timestamp={ts}&refreshToken={quote(refresh_token, safe='')}&sign={sign}"
        )
        try:
            async with self._session.post(
                url, headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            self.tokens.last_refresh_error = f"刷新端点返回 HTTP {err.status}"
            return False
        except (aiohttp.ClientError, json.JSONDecodeError, TimeoutError, asyncio.TimeoutError) as err:
            self.tokens.last_refresh_error = f"网络错误：{err}"
            return False

        new_access = data.get("access_token") if isinstance(data, dict) else None
        if not new_access:
            safe = data if isinstance(data, dict) else {}
            rc = safe.get("resultCode") or safe.get("result_code")
            msg = safe.get("resultMsg") or safe.get("result_msg") or ""
            if rc:
                self.tokens.last_refresh_error = f"服务端拒绝 resultCode={rc} {msg}".strip()
            else:
                self.tokens.last_refresh_error = f"响应异常：{str(data)[:120]}"
            return False

        self.tokens.access_token = new_access
        if data.get("refresh_token"):
            self.tokens.refresh_token = data["refresh_token"]
        self.tokens.expires_at = time.time() + int(
            data.get("expires_in") or DEFAULT_TOKEN_EXPIRES_IN
        )
        self.tokens.last_refresh_error = None
        _LOGGER.info("微信 OAuth token 已刷新，有效期 %s 秒", data.get("expires_in"))
        return True

    async def async_refresh_if_near_expiry(self) -> bool:
        """临近过期时主动刷新；返回是否真的发起了刷新。"""
        if not self.tokens.expires_at or self.tokens.is_near_expiry():
            return await self.async_try_refresh_token()
        return False

    async def async_ensure_token(self) -> bool:
        """确保有可用 token：临近过期则刷新。返回当前是否有可用 access_token。"""
        if not self.tokens.access_token:
            return False
        if self.tokens.is_near_expiry():
            return await self.async_try_refresh_token()
        return True

    # ------------------------------------------------------------------
    # Signed business calls on /nv1/vcc-cbs/*
    # ------------------------------------------------------------------
    async def _cbs_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        _retried: bool = False,
    ) -> dict[str, Any]:
        """Signed GET on the WeChat-central VCC gateway.

        Attaches ``Authorization: Bearer <access_token>`` and a ``timestamp``+
        ``sign`` signature. On HTTP 401 (or a resultCode auth error) refreshes
        once and retries with a fresh token/sign.
        """
        if not await self.async_ensure_token():
            raise TownGasAuthError("无可用 access_token，且 refresh 失败")

        ts = int(time.time() * 1000)
        all_params: dict[str, Any] = {**(params or {}), "timestamp": ts}
        all_params["sign"] = _sign(all_params)
        url = f"{self._base_url}{WECHAT_API_PATH}{path}?{urlencode(all_params)}"
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.tokens.access_token}",
        }
        _LOGGER.debug("CBS GET %s", path)
        try:
            async with self._session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise TownGasApiError(f"网络错误: {err}") from err

        if resp.status == 401:
            if not _retried and await self.async_try_refresh_token():
                return await self._cbs_get(path, params, _retried=True)
            raise TownGasAuthError("access_token 过期且 refresh 失败")

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        result_code = data.get("resultCode") if isinstance(data, dict) else None
        if result_code is not None and str(result_code) in AUTH_ERROR_CODES:
            if not _retried and await self.async_try_refresh_token():
                return await self._cbs_get(path, params, _retried=True)
            raise TownGasAuthError(data.get("resultMsg") or "access token 过期")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def async_validate_token(self) -> list[dict[str, Any]]:
        """Validate the token (liveness check) — returns an empty list.

        v1.5.0: the previous auto-discovery via ``queryBindList`` required an
        org parameter that cannot be discovered inside the integration (the
        server rejects any guessed value), so it is no longer used. We instead
        do a lightweight liveness probe via ``getLoginUserInfo`` (no org needed)
        to confirm the token is still accepted; the return value is unused.
        """
        await self.async_get_user_info()
        return []

    async def async_get_bound_subs(self) -> list[dict[str, Any]]:
        """户号管理：返回本账户绑定的户号列表（已归一化为 subsCode/orgCode）。"""
        data = await self._cbs_get("/usersubs/queryBindList")
        datas = data.get("datas") or []
        return [self._norm_subs(s) for s in datas]

    async def async_get_sub_info(
        self, subs_code: str, org_code: str
    ) -> dict[str, Any]:
        """Get subscription (气户) info: name / address / org."""
        data = await self._cbs_get(
            "/usersubs/subsDetailByCode",
            {"subsCode": subs_code, "orgCode": org_code},
        )
        datas = data.get("datas") or []
        return self._norm_subs(datas[0]) if datas else {
            "subsCode": subs_code, "orgCode": org_code,
        }

    async def async_get_bills(
        self,
        subs_id: str | None = None,
        subs_code: str | None = None,
        org_code: str | None = None,
        page_index: int = 1,
        page_size: int = 24,
    ) -> list[dict[str, Any]]:
        """历史账单（charge/queryHistoryFee），归一化为 营业厅字段名。

        vcc-cbs 的 queryHistoryFee 认 ``subsId``（气户标识），与 preCheck 一致；
        传 subsCode+orgCode 会返回「气户标识不能为空」。优先用 subs_id。

        实测响应结构：``datas`` 是「账期」数组，每个账期含 ``gasFeeList``
        （具体账单明细，里面有 amount/price/chrgSum/lastReading/currReading
        等字段）。所以这里把 ``gasFeeList`` 摊平为一条条账单再归一化。
        """
        if subs_id:
            params = {"subsId": subs_id, "pageIndex": page_index, "pageSize": page_size}
        elif subs_code and org_code:
            params = {
                "subsCode": subs_code,
                "orgCode": org_code,
                "pageIndex": page_index,
                "pageSize": page_size,
            }
        else:
            return []
        data = await self._cbs_get("/charge/queryHistoryFee", params)
        periods = data.get("datas") or []
        bills: list[dict[str, Any]] = []
        for period in periods:
            if not isinstance(period, dict):
                continue
            period_ym = period.get("yrMonth")
            for item in (period.get("gasFeeList") or []):
                if not isinstance(item, dict):
                    continue
                b = self._norm_bill(item)
                # 账期（YYYY-MM）来自外层，便于展示/排序兜底
                b["periodMonth"] = period_ym
                bills.append(b)
        try:
            bills.sort(key=lambda b: str(b.get("yrMonth", "")), reverse=True)
        except TypeError:
            pass
        return bills

    async def async_get_balance(
        self,
        subs_id: str | None = None,
        subs_code: str | None = None,
        org_code: str | None = None,
    ) -> dict[str, Any] | None:
        """账户余额/应付/上次抄表日（charge/gasFeeBaseinfo）。

        实测响应为**平铺结构**（无 ``datas`` 包裹）：

        * ``availableBalance`` / ``balance``   → 可用余额（元）
        * ``feePayable``                       → 应付费用（元，预付费为 ``0``）
        * ``lastMeterReadingDate``             → 上次抄表日期

        优先用 subsId（气户标识），与 preCheck 一致；无 subs_id 时回退
        subsCode+orgCode。
        """
        if subs_id:
            params = {"subsId": subs_id}
        elif subs_code and org_code:
            params = {"subsCode": subs_code, "orgCode": org_code}
        else:
            return None
        data = await self._cbs_get("/charge/gasFeeBaseinfo", params)
        if not isinstance(data, dict):
            return None
        # 平铺结构：直接取顶层字段
        available = (
            data.get("availableBalance")
            or data.get("balance")
            or data.get("acctBalance")
            or data.get("resBalance")
        )
        fee_payable = data.get("feePayable") or data.get("feePayableAmt")
        return {
            "available_balance": _to_float(available),
            "fee_payable": _to_float(fee_payable),
            "last_meter_reading_date": data.get("lastMeterReadingDate"),
        }

    async def async_get_unpaid(self, subs_code: str, org_code: str) -> dict[str, Any]:
        """欠费摘要。v1.5.0 改为从账单列表推导（见 coordinator），此处保留兼容。

        直接返回空结构；coordinator 会基于 bills 计算 arrears / unpaid_count。
        """
        return {}

    async def async_get_reading(
        self,
        subs_id: str | None = None,
        subs_code: str | None = None,
        org_code: str | None = None,
    ) -> dict[str, Any] | None:
        """本期表数（charge/preCheck → currReading）。

        优先用 ``subsId``（杭州版逆向实测字段名，最稳）；当仅持有
        subsCode+orgCode（无 subsId）时回退到该组合。两种都失败返回 None。
        """
        attempts = []
        if subs_id:
            attempts.append(("/charge/preCheck (subsId)", {"subsId": subs_id}))
        if subs_code and org_code:
            attempts.append(
                ("/charge/preCheck (subsCode+orgCode)",
                 {"subsCode": subs_code, "orgCode": org_code})
            )
        for label, params in attempts:
            try:
                data = await self._cbs_get("/charge/preCheck", params)
            except (TownGasAuthError, TownGasApiError) as err:
                _LOGGER.debug("%s 失败: %s", label, err)
                continue
            datas = data.get("datas") if isinstance(data, dict) else None
            if not isinstance(datas, dict):
                continue
            lst = datas.get("readingRptList") or []
            first = lst[0] if (lst and isinstance(lst[0], dict)) else {}
            curr = first.get("currReading")
            last = first.get("lastReading")
            if curr is None and last is None:
                # 该组合无读数，尝试下一个
                continue
            return {
                "currReading": curr,
                "lastReading": last,
                "source": label,
                # preCheck 同响应里还带了账户级字段，预付费户尤其有用：
                # savingSum=充值余额（元）、totalFee=本期气费、chargeType=prepay/postpay
                "savingSum": datas.get("savingSum"),
                "totalFee": datas.get("totalFee"),
                "chargeType": datas.get("chargeType"),
            }
        return None

    async def async_get_user_info(self) -> dict[str, Any]:
        """轻量鉴权存活校验：getLoginUserInfo 不需要 org 参数。

        返回加密的 encryptData（明文解密非必需）；只要不返回鉴权错误即说明
        token 仍有效。用于配置流/健康检查的 liveness 探测，替代需要 org 的
        queryBindList（后者在集成内无法自动发现 org，会卡死配置）。
        """
        return await self._cbs_get("/usersubs/getLoginUserInfo")

    async def async_dump_raw(self, sub: dict[str, Any]) -> dict[str, Any]:
        """诊断用：返回各接口**原始**响应，便于校准字段名与金额单位。

        不依赖任何归一化；调用方（dump_raw 服务）把结果贴回即可分析。
        每个接口单独 try，互不拖累；失败时记录 ERR 字符串。
        """
        subs_id = sub.get("subs_id")
        subs_code = sub.get("subs_code")
        org_code = sub.get("org_code")
        out: dict[str, Any] = {}

        if subs_id:
            try:
                out["preCheck_subsId"] = await self._cbs_get(
                    "/charge/preCheck", {"subsId": subs_id}
                )
            except Exception as err:  # noqa: BLE001
                out["preCheck_subsId"] = f"ERR {err}"
        if subs_code and org_code:
            try:
                out["preCheck_subsCode"] = await self._cbs_get(
                    "/charge/preCheck",
                    {"subsCode": subs_code, "orgCode": org_code},
                )
            except Exception as err:  # noqa: BLE001
                out["preCheck_subsCode"] = f"ERR {err}"
        # queryHistoryFee / gasFeeBaseinfo 认 subsId（气户标识），与 preCheck 一致；
        # 传 subsCode+orgCode 会返回「气户标识不能为空」。优先用 subs_id。
        if subs_id:
            fee_params: dict[str, Any] = {"subsId": subs_id}
            bal_params: dict[str, Any] = {"subsId": subs_id}
        elif subs_code and org_code:
            fee_params = {
                "subsCode": subs_code,
                "orgCode": org_code,
                "pageIndex": 1,
                "pageSize": 2,
            }
            bal_params = {"subsCode": subs_code, "orgCode": org_code}
        else:
            fee_params = bal_params = {}
        if fee_params:
            try:
                out["queryHistoryFee"] = await self._cbs_get(
                    "/charge/queryHistoryFee", fee_params
                )
            except Exception as err:  # noqa: BLE001
                out["queryHistoryFee"] = f"ERR {err}"
        if bal_params:
            try:
                out["gasFeeBaseinfo"] = await self._cbs_get(
                    "/charge/gasFeeBaseinfo", bal_params
                )
            except Exception as err:  # noqa: BLE001
                out["gasFeeBaseinfo"] = f"ERR {err}"
        try:
            out["getLoginUserInfo"] = await self._cbs_get(
                "/usersubs/getLoginUserInfo"
            )
        except Exception as err:  # noqa: BLE001
            out["getLoginUserInfo"] = f"ERR {err}"
        return out

    # ------------------------------------------------------------------
    # Field normalization（双命名兜底 + 原始字段透传）
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_subs(s: dict[str, Any]) -> dict[str, Any]:
        out = dict(s)
        out["subsCode"] = s.get("subsCode") or s.get("subsId") or s.get("userSubsCode") or s.get("subCode")
        out["orgCode"] = s.get("orgCode") or s.get("orgId") or s.get("subOrgCode")
        out["name"] = s.get("name") or s.get("subsName") or s.get("userName")
        out["displayAddr"] = (
            s.get("displayAddr") or s.get("addr")
            or s.get("subsAddr") or s.get("address") or s.get("subAddr")
        )
        out["orgName"] = s.get("orgName") or s.get("orgShortName")
        return out

    @staticmethod
    def _norm_bill(b: dict[str, Any]) -> dict[str, Any]:
        """把 vcc-cbs 账单字段归一化为 营业厅传感器使用的字段名。

        同时**原样透传所有原始字段**（out 先 copy 整条），保证即使下方兜底
        没命中真实字段名，传感器属性里仍能看到原始数据，便于探测后校准。
        """
        out = dict(b)
        # 账期（YYYYMM）
        out["yrMonth"] = (
            b.get("yrMonth") or b.get("billMonth") or b.get("month")
            or b.get("accountMonth") or b.get("billYm")
        )
        # 用气量（m³）
        out["amount"] = (
            b.get("amount") or b.get("useGas") or b.get("gasUsage")
            or b.get("gasAmount")
        )
        # 表数
        out["currReading"] = (
            b.get("currReading") or b.get("currentReading") or b.get("endReading")
        )
        out["lastReading"] = (
            b.get("lastReading") or b.get("prevReading") or b.get("startReading")
        )
        # 金额（营业厅单位为「分」；vcc-cbs 单位待探测确认，原值透传，不在此处换算）
        out["chrgSum"] = (
            b.get("chrgSum") or b.get("billFee") or b.get("billAmount")
            or b.get("chargeSum") or b.get("totalFee") or b.get("amt")
        )
        # 欠费金额
        out["totalUnpaidFee"] = (
            b.get("totalUnpaidFee") or b.get("unpaidFee") or b.get("oweFee")
            or b.get("arrearsFee")
        )
        # 违约金
        out["unpaidLateFee"] = b.get("unpaidLateFee") or b.get("lateFee")
        # 阶梯明细
        out["stepFeeResults"] = (
            b.get("stepFeeResults") or b.get("stepList")
            or b.get("stepFeeList") or b.get("stepRslt") or []
        )
        return out
