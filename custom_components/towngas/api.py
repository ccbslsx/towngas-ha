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
* Token 刷新走平台级 oauth 服务（weixin.towngasvcc.com），与城市业务 host 解耦；
  签名 = MD5(排序的 key+value 拼接 + 盐 SIGN_SALT) 转大写。
"""

from __future__ import annotations

import asyncio
import hashlib
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
    DEFAULT_TOKEN_EXPIRES_IN,
    LOGGER,
    OAUTH_REFRESH_URL,
    SIGN_SALT,
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


def _sign(params: dict[str, Any]) -> str:
    """平台级签名：排序的 key+value 拼接 + 盐 SIGN_SALT，MD5 转大写。

    sign 字段本身不参与签名；空字符串 / None 的值也排除。
    """
    keys = sorted(
        k
        for k, v in params.items()
        if k != "sign" and v is not None and v != ""
    )
    raw = "".join(f"{k}{params[k]}" for k in keys)
    return hashlib.md5((raw + SIGN_SALT).encode()).hexdigest().upper()


def _extract_token_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """从刷新响应里取有效的 token 载荷（扁平或嵌套于 datas）。"""
    if not isinstance(data, dict):
        return None
    if data.get("access_token"):
        return data
    datas = data.get("datas")
    if isinstance(datas, dict) and datas.get("access_token"):
        return datas
    return None


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
                url += "&" + "&".join(
                    f"{k}={str(v)}" for k, v in clean.items()
                )
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
            LOGGER.debug("no refresh_token available; cannot refresh")
            return False

        ts = int(time.time() * 1000)
        params = {"timestamp": ts, "refreshToken": refresh_token}
        params["sign"] = _sign(params)
        url = f"{OAUTH_REFRESH_URL}?{urlencode(params)}"

        try:
            async with self._session.post(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            LOGGER.debug("Token refresh HTTP %s", err.status)
            return False
        except (aiohttp.ClientError, json.JSONDecodeError, TimeoutError) as err:
            LOGGER.debug("Token refresh network error: %s", err)
            return False

        payload = _extract_token_payload(data)
        if not payload:
            rc = data.get("resultCode") or data.get("result_code")
            if rc:
                # 业务级错误（如 90143 refreshToken 已失效）→ 确定性刷新失败
                LOGGER.warning(
                    "Token 刷新失败 resultCode=%s msg=%s",
                    rc,
                    data.get("resultMsg") or data.get("resultMsg"),
                )
            else:
                LOGGER.warning("Token 刷新返回异常响应: %s", data)
            return False

        new_access = payload.get("access_token")
        if not new_access or new_access == self.tokens.access_token:
            return False

        self.tokens.access_token = new_access
        if payload.get("refresh_token"):
            self.tokens.refresh_token = payload["refresh_token"]
        expires_in = int(payload.get("expires_in") or DEFAULT_TOKEN_EXPIRES_IN)
        self.tokens.expires_at = time.time() + expires_in
        LOGGER.info("Towngas access token 已刷新，有效期 %s 秒", expires_in)
        return True
