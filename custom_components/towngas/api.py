"""API client for the Towngas (港华燃气) 网上营业厅 openapi.

Reverse engineered from https://maanshan.towngasvcc.com frontend:

* All data endpoints are GET requests.
* URL pattern:
    {base}/openapi/uv1/<path>?seq=<seq>&token=<access_token>&<params...>
  where seq = zero-padded 5-digit interface code + YYYYMMDDHHmmss + 13 random digits.
* Response JSON always contains resultCode ("0" = success) and resultMsg.
* resultCode 20001 / 40058 mean the token is invalid or expired.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime
from typing import Any

import aiohttp

from .const import AUTH_ERROR_CODES, CLIENT_ID, LOGGER

_LOGGER = logging.getLogger(__name__)

# Interface codes (from the frontend service modules)
CODE_QUERY_SUBS = 3505
CODE_QUERY_BILLS = 3516
CODE_QUERY_ACCT_RES = 3509
CODE_QUERY_UNPAID_BILLS = 3514
CODE_QUERY_LAST_READINGS = 3511
CODE_QUERY_BIND_SUBS = 3529
CODE_OAUTH_TOKEN = 1502


class TownGasAuthError(Exception):
    """Raised when the access token is invalid or expired."""


class TownGasApiError(Exception):
    """Raised for any other API error."""


class TokenStore:
    """Mutable holder so the client can use refreshed tokens."""

    def __init__(self, access_token: str, refresh_token: str | None = None) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token


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
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise TownGasApiError("连接港华燃气服务器超时") from err
        except aiohttp.ClientError as err:
            raise TownGasApiError(f"网络错误: {err}") from err
        except json.JSONDecodeError as err:
            raise TownGasApiError("服务器返回了无法解析的数据") from err

        result_code = str(data.get("resultCode", ""))
        if result_code in AUTH_ERROR_CODES:
            raise TownGasAuthError(data.get("resultMsg") or "access token 过期")
        if result_code != "0":
            raise TownGasApiError(
                f"接口错误 {result_code}: {data.get('resultMsg', '')}"
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
        """Best-effort attempt to refresh the access token.

        The web frontend never refreshes programmatically, so the exact
        contract is unverified. Returns True and updates the token store
        on success.
        """
        refresh_token = self.tokens.refresh_token
        if not refresh_token:
            return False
        url = (
            f"{self._base_url}/openapi/uv1/oauth/token"
            f"?seq={_build_seq(CODE_OAUTH_TOKEN)}"
            f"&client_id={CLIENT_ID}"
            f"&refresh_token={refresh_token}"
            "&grant_type=refresh_token&scope=read%20write"
        )
        try:
            async with self._session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, json.JSONDecodeError, TimeoutError):
            return False

        if str(data.get("resultCode", "")) != "0":
            LOGGER.debug("Token refresh failed: %s", data)
            return False

        payload = data.get("datas") or data
        new_token = (
            payload.get("access_token")
            if isinstance(payload, dict)
            else None
        ) or (data.get("access_token"))
        if not new_token or new_token == self.tokens.access_token:
            return False
        self.tokens.access_token = new_token
        if isinstance(payload, dict) and payload.get("refresh_token"):
            self.tokens.refresh_token = payload["refresh_token"]
        LOGGER.info("Towngas access token refreshed")
        return True
