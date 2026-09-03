"""API client for the Towngas (港华燃气) 网上营业厅 openapi.

Reverse engineered from https://maanshan.towngasvcc.com frontend:

* All data endpoints are GET requests.
* URL pattern:
    {base}/openapi/uv1/<path>?seq=<seq>&token=<access_token>&<params...>
  where seq = zero-padded 5-digit interface code + YYYYMMDDHHmmss + 13 random digits.
* Response JSON: successful calls return the data payload directly
  (e.g. ``{"datas": [...], ...}``) WITHOUT a ``resultCode`` key. Only error
  responses carry a ``resultCode`` (e.g. ``20001`` = access token expired).
* resultCode 20001 / 40058 mean the token is invalid or expired.
* Token 刷新走**业务 host 自己的标准 OAuth2 端点**
  ``{base}/openapi/uv1/oauth/token?grant_type=refresh_token&...``，
  响应为 ``{"access_token","token_type","refresh_token","expires_in","scope"}``。
  实测马鞍山：access_token 寿命仅 899 秒，refresh_token **不轮换**（可永久复用）。
  ⚠️ 不要使用 weixin.towngasvcc.com/vcc-oauth（微信小程序那套 oauth），
  它与营业厅的 client_id 不互通，刷新恒定返回 90143「refreshToken已失效」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import (
    AUTH_ERROR_CODES,
    CLIENT_ID,
    CODE_OAUTH_TOKEN,
    DEFAULT_TOKEN_EXPIRES_IN,
    LOGGER,
    OAUTH_CODE2TOKEN_PATH,
    OAUTH_CODE_PARAM,
    OAUTH_GRANT_TYPE_REFRESH,
    OAUTH_REDIRECT_URI_PATH,
    OAUTH_SCOPE,
    OAUTH_TOKEN_PATH,
    TOKEN_EXPIRY_BUFFER_SECS,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Interface codes (from the frontend service modules)
CODE_QUERY_SUBS = 3505
CODE_QUERY_BILLS = 3516
CODE_QUERY_ACCT_RES = 3509
CODE_QUERY_UNPAID_BILLS = 3514
CODE_QUERY_LAST_READINGS = 3511
CODE_QUERY_BIND_SUBS = 3529


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
        # 最近一次刷新失败的原因（成功时置 None）。用于在界面/服务调用里
        # 暴露刷新到底为什么失败，避免用户只看到"又过期了"而无从排查。
        self.last_refresh_error: str | None = None

    def is_near_expiry(self, buffer: int = TOKEN_EXPIRY_BUFFER_SECS) -> bool:
        """True 当已知过期时间且已接近过期（提前 buffer 秒）。"""
        if not self.expires_at:
            return False
        return (self.expires_at - buffer) <= time.time()


def _build_seq(code: int) -> str:
    """seq = 5-digit zero padded code + timestamp + 13 random digits."""
    return (
        f"{code:05d}"
        + datetime.now().strftime("%Y%m%d%H%M%S")
        + f"{random.randint(0, 10**13 - 1):013d}"
    )


class TownGasApiClient:
    """Thin async client around the 网上营业厅 openapi."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token_store: TokenStore,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self.tokens = token_store

    # ------------------------------------------------------------------
    def _build_url(
        self,
        code: int,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> str:
        url = f"{self._base_url}{path}"
        url += f"?seq={_build_seq(code)}"
        if authenticated:
            url += f"&token={self.tokens.access_token}"
        url += f"&client_id={CLIENT_ID}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                # 必须做 URL 编码：oauth 端点的 scope 值为 "read write"（含空格），
                # 直接拼进 URL 会产生非法 URL（http.client / aiohttp 均会拒绝）。
                url += "&" + urlencode(clean)
        return url

    async def _get(
        self,
        code: int,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        url = self._build_url(code, path, params, authenticated=authenticated)
        _LOGGER.debug("GET %s", url)
        try:
            async with self._session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            raise TownGasApiError(
                f"服务器返回 HTTP {err.status}: {err.message}"
            ) from err
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise TownGasApiError("连接港华燃气服务器超时") from err
        except aiohttp.ClientError as err:
            raise TownGasApiError(f"网络错误: {err}") from err
        except json.JSONDecodeError as err:
            raise TownGasApiError("服务器返回了无法解析的数据") from err

        # The API only attaches ``resultCode`` on *error* responses. Successful
        # responses return the payload directly (no resultCode key at all).
        # So we must NOT require resultCode == "0" — doing so rejects every
        # valid response and surfaces a misleading "cannot connect" error.
        result_code = data.get("resultCode")
        if result_code is not None:
            rc = str(result_code)
            if rc in AUTH_ERROR_CODES:
                raise TownGasAuthError(data.get("resultMsg") or "access token 过期")
            if rc != "0":
                raise TownGasApiError(
                    f"接口错误 {rc}: {data.get('resultMsg', '')}"
                )
        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def async_validate_token(self) -> list[dict[str, Any]]:
        """Validate the token and return the list of bound subscriptions."""
        return await self.async_get_bound_subs()

    async def async_get_bound_subs(self) -> list[dict[str, Any]]:
        """Get subscriptions bound to this account (户号管理)."""
        data = await self._get(
            CODE_QUERY_BIND_SUBS,
            "/openapi/uv1/user/queryBindSubsLimitServer",
            {"isPayOrReport": "Y"},
        )
        return data.get("datas") or []

    async def async_get_sub_info(
        self, subs_code: str, org_code: str
    ) -> dict[str, Any]:
        """Get subscription (气户) info: name / address / org."""
        data = await self._get(
            CODE_QUERY_SUBS,
            "/openapi/uv1/subs/querySubs",
            {"subsCode": subs_code, "orgCode": org_code},
        )
        datas = data.get("datas") or []
        return datas[0] if datas else {}

    async def async_get_bills(
        self,
        subs_code: str,
        org_code: str,
        page_index: int = 1,
        page_size: int = 24,
    ) -> list[dict[str, Any]]:
        """Get bill history (历史账单), newest first."""
        data = await self._get(
            CODE_QUERY_BILLS,
            "/openapi/uv1/bill/queryBills",
            {
                "subsCode": subs_code,
                "orgCode": org_code,
                "pageIndex": page_index,
                "pageSize": page_size,
            },
        )
        bills = data.get("datas") or []
        # Defensive: make sure newest first.
        try:
            bills.sort(key=lambda b: str(b.get("yrMonth", "")), reverse=True)
        except TypeError:  # pragma: no cover
            pass
        return bills

    async def async_get_balance(self, subs_code: str, org_code: str) -> float | None:
        """Get account balance (账户余额 / 气费余额)."""
        data = await self._get(
            CODE_QUERY_ACCT_RES,
            "/openapi/uv1/acct/queryAcctRes",
            {"subsCode": subs_code, "orgCode": org_code, "acctResType": 2},
        )
        datas = data.get("datas") or []
        if datas:
            try:
                # 接口余额单位为「分」，转换为「元」
                return round(float(datas[0].get("balance")) / 100.0, 2)
            except (TypeError, ValueError):
                return None
        return None

    async def async_get_unpaid(
        self, subs_code: str, org_code: str
    ) -> dict[str, Any]:
        """Get unpaid bill summary (欠费)."""
        return await self._get(
            CODE_QUERY_UNPAID_BILLS,
            "/openapi/uv1/bill/queryUnpaidBills",
            {"subsCode": subs_code, "orgCode": org_code},
        )

    async def async_try_refresh_token(self) -> bool:
        """Use refresh_token to obtain a fresh access_token (平台级 oauth).

        Returns True and updates the token store (access_token /
        refresh_token / expires_at) on success. On failure returns False —
        the caller should then fall back to reauth.
        """
        refresh_token = self.tokens.refresh_token
        if not refresh_token:
            self.tokens.last_refresh_error = (
                "未保存 refresh_token（粘贴内容里没有 refresh_token 字段）"
            )
            LOGGER.warning("Token 刷新失败：%s", self.tokens.last_refresh_error)
            return False

        # 城市级标准 OAuth2 端点：{业务host}/openapi/uv1/oauth/token
        # 实测有效，且 refresh_token 不轮换（可永久复用）。
        redirect_uri = f"{self._base_url}{OAUTH_REDIRECT_URI_PATH}"
        url = self._build_url(
            CODE_OAUTH_TOKEN,
            OAUTH_TOKEN_PATH,
            {
                "grant_type": OAUTH_GRANT_TYPE_REFRESH,
                "refresh_token": refresh_token,
                "scope": OAUTH_SCOPE,
                "redirect_uri": redirect_uri,
            },
            authenticated=False,
        )

        try:
            async with self._session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            self.tokens.last_refresh_error = f"刷新端点返回 HTTP {err.status}"
            LOGGER.warning("Token 刷新失败：%s", self.tokens.last_refresh_error)
            return False
        except (aiohttp.ClientError, json.JSONDecodeError, TimeoutError) as err:
            self.tokens.last_refresh_error = f"网络错误：{err}"
            LOGGER.warning("Token 刷新失败：%s", self.tokens.last_refresh_error)
            return False

        # 成功响应形如 {"access_token","token_type","refresh_token","expires_in",
        # "scope"}，不带 resultCode；只有错误响应才带 resultCode。
        new_access = data.get("access_token") if isinstance(data, dict) else None
        if not new_access:
            rc = (data or {}).get("resultCode") or (data or {}).get("result_code")
            msg = (data or {}).get("resultMsg") or (data or {}).get("result_msg") or ""
            if rc:
                self.tokens.last_refresh_error = (
                    f"服务端拒绝 resultCode={rc} {msg}".strip()
                )
            else:
                self.tokens.last_refresh_error = f"响应异常：{str(data)[:120]}"
            LOGGER.warning("Token 刷新失败：%s", self.tokens.last_refresh_error)
            return False

        # 注意：token 尚未到期时刷新，服务端可能原样返回同一个 access_token。
        # 旧实现据此判为失败，导致 expires_at 一直是 0、is_near_expiry() 永远
        # 为 False，主动续期机制等于被禁用，只能等 token 真正过期后被动刷新。
        # 这里放宽：只要服务端正常响应就采信，并更新 expires_at。
        rotated = new_access != self.tokens.access_token
        self.tokens.access_token = new_access
        if data.get("refresh_token"):
            self.tokens.refresh_token = data["refresh_token"]
        expires_in = int(data.get("expires_in") or DEFAULT_TOKEN_EXPIRES_IN)
        self.tokens.expires_at = time.time() + expires_in
        self.tokens.last_refresh_error = None
        LOGGER.info(
            "Towngas access token 已刷新，有效期 %s 秒（%s）",
            expires_in,
            "换发新 token" if rotated else "复用原 token",
        )
        return True

    async def async_exchange_token_code(self, token_code: str) -> bool:
        """用营业厅登录后浏览器重定向带回的 tokenCode 换发 access_token + refresh_token。

        对应营业厅前端 ``weboauth2Code2Token`` 端点（登录成功 → ``/loginRedirect?...&
        tokenCode=XXX`` → 调此端点）。返回 True 并更新 token store（access_token /
        refresh_token / expires_at）；失败返回 False（调用方应回退到 reauth 提示）。

        响应结构与 refresh 端点一致：``{"access_token","token_type",
        "refresh_token","expires_in","scope"}``，不带 resultCode。
        """
        if not token_code:
            self.tokens.last_refresh_error = "未提供 tokenCode（登录后地址栏里的 tokenCode 参数）"
            LOGGER.warning("tokenCode 换发失败：%s", self.tokens.last_refresh_error)
            return False

        url = self._build_url(
            CODE_OAUTH_TOKEN,
            OAUTH_CODE2TOKEN_PATH,
            {OAUTH_CODE_PARAM: token_code},
            authenticated=False,
        )

        try:
            async with self._session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            self.tokens.last_refresh_error = f"换发端点返回 HTTP {err.status}"
            LOGGER.warning("tokenCode 换发失败：%s", self.tokens.last_refresh_error)
            return False
        except (aiohttp.ClientError, json.JSONDecodeError, TimeoutError) as err:
            self.tokens.last_refresh_error = f"网络错误：{err}"
            LOGGER.warning("tokenCode 换发失败：%s", self.tokens.last_refresh_error)
            return False

        new_access = data.get("access_token") if isinstance(data, dict) else None
        if not new_access:
            rc = (data or {}).get("resultCode") or (data or {}).get("result_code")
            msg = (data or {}).get("resultMsg") or (data or {}).get("result_msg") or ""
            if rc:
                self.tokens.last_refresh_error = (
                    f"服务端拒绝 resultCode={rc} {msg}".strip()
                )
            else:
                self.tokens.last_refresh_error = f"响应异常：{str(data)[:120]}"
            LOGGER.warning("tokenCode 换发失败：%s", self.tokens.last_refresh_error)
            return False

        self.tokens.access_token = new_access
        if data.get("refresh_token"):
            self.tokens.refresh_token = data["refresh_token"]
        expires_in = int(data.get("expires_in") or DEFAULT_TOKEN_EXPIRES_IN)
        self.tokens.expires_at = time.time() + expires_in
        self.tokens.last_refresh_error = None
        LOGGER.info(
            "tokenCode 换发成功，有效期 %s 秒",
            expires_in,
        )
        return True

    async def async_refresh_if_near_expiry(self) -> bool:
        """临近过期时主动刷新；返回是否真的发起了刷新。

        借鉴杭州项目的 ensure_token 思路：在真正发业务请求之前先续期，
        避免把请求打在已经过期的 token 上。

        两种情况下会刷新：
        1. 已知过期时间且临近过期（剩余寿命 < TOKEN_EXPIRY_BUFFER_SECS）；
        2. 过期时间未知（expires_at == 0，例如首次启动或旧配置升级）——
           此时不能假设 token 还新鲜，必须显式续期，否则会带着已过期的
           token 去请求。
        """
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
