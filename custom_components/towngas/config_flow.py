"""Config flow for the Towngas (港华燃气) integration — v1.5.0 WeChat OAuth."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote, unquote

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import TokenStore, TownGasApiClient, TownGasApiError, TownGasAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BASE_URL,
    CONF_REFRESH_TOKEN,
    CONF_SUBSCRIPTIONS,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
    OPT_SCAN_INTERVAL,
    OPT_TOKEN_REFRESH_INTERVAL,
    SCAN_INTERVAL_MAX,
    SCAN_INTERVAL_MIN,
    TOKEN_REFRESH_INTERVAL_MAX,
    TOKEN_REFRESH_INTERVAL_MIN,
    WECHAT_CLIENT_ID,
    WECHAT_HOST,
    WECHAT_OAUTH_PATH,
    WECHAT_REDIRECT_URI,
)

_LOGGER = logging.getLogger(__name__)


def wechat_login_url() -> str:
    """Construct the WeChat OAuth login URL (open in WeChat to scan/log in)."""
    ru = quote(WECHAT_REDIRECT_URI, safe="")
    return (
        f"https://{WECHAT_HOST}{WECHAT_OAUTH_PATH}/oauth/authorize2/union"
        f"?clientid={WECHAT_CLIENT_ID}&redirectUri={ru}"
    )


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): str,
        vol.Optional(
            CONF_BASE_URL, description={"suggested_value": DEFAULT_BASE_URL}
        ): str,
    }
)


def _truncate(msg: str, limit: int = 240) -> str:
    msg = str(msg or "").strip()
    if len(msg) > limit:
        msg = msg[:limit].rstrip() + "…"
    return msg


def _format_detail(err: Exception) -> str:
    """Render an exception into a short detail suffix for the form description."""
    return f"\n\n原因：{_truncate(err)}"


@callback
def _async_warn_refresh_unusable(hass, error: str | None) -> None:
    """Tell the user right away that the pasted refresh_token cannot refresh."""
    persistent_notification.async_create(
        hass,
        "新的 access_token 已保存，但**自动刷新测试没有通过**：\n\n"
        f"`{_truncate(error or '未知原因', 300)}`\n\n"
        "这意味着 token 到期后仍可能再次要求你重新登录。微信 OAuth 正常情况"
        "下 refresh_token 可稳定多日，请确认粘贴的是微信登录回跳地址里的 "
        "`?code=`（一次性），或完整的 token JSON（含 refresh_token）。\n\n"
        "可在「开发者工具 → 服务」调用 `towngas.force_refresh_token` 复测。",
        title="港华燃气：token 自动刷新不可用",
        notification_id=f"{DOMAIN}_refresh_unusable",
    )


def _extract_auth_code(raw: str) -> str | None:
    """从粘贴内容里抽取微信登录授权码（authCode / code）。

    支持形态：
      * 完整重定向 URL：``https://.../h5-gas/?code=abc`` 或 ``?authCode=abc``
      * JSON：``{"authCode":"abc"}`` / ``{"code":"abc"}``
      * 裸授权码字符串

    若内容明显是 token JSON（含 access_token）则返回 None，交由 token-JSON 分支处理。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    decoded = unquote(raw)
    # JSON 形态
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        if obj:
            if obj.get("access_token"):
                return None  # 这是 token JSON，走 refresh 分支
            code = obj.get("authCode") or obj.get("code")
            if code:
                return str(code)
        # JSON 被拷贝损坏：正则兜底
        m = re.search(r'"(?:authCode|code)"\s*:\s*"([^"]+)"', raw)
        if m:
            return m.group(1)
        return None
    # URL 形态
    patterns = [
        r'[?&]authCode=([^&\s"\'}>]+)',
        r'[?&]code=([^&\s"\'}>]+)',
    ]
    for pat in patterns:
        m = re.search(pat, decoded) or re.search(pat, raw)
        if m:
            code = m.group(1).strip().strip('"{}')
            if code:
                return code
    # 裸授权码（非 JSON、不含 token）
    if "access_token" not in raw and len(raw) < 120:
        return raw
    return None


def _parse_token_json(raw: str) -> tuple[str, str | None]:
    """Accept a full token JSON (access_token + optional refresh_token)."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            access = obj.get("access_token") or obj.get("token") or ""
            refresh = obj.get("refresh_token") or None
            if access:
                return access, refresh
        except json.JSONDecodeError:
            pass
        m = re.search(r'"access_token"\s*:\s*"([^"]+)"', raw)
        if m:
            r = re.search(r'"refresh_token"\s*:\s*"([^"]+)"', raw)
            return m.group(1), r.group(1) if r else None
        raise ValueError("无法从输入中解析 access_token")
    return raw, None


async def _finalize_tokens(
    hass, base_url: str, raw: str
) -> dict[str, Any]:
    """Obtain usable tokens from either:

    1. a pasted **WeChat auth code** (``code``/``authCode`` from the login
       redirect URL after scanning/logging in via WeChat) — exchanged via
       ``accessToekn?authCode=``; OR
    2. a pasted **access_token (+ optional refresh_token)** / full token JSON —
       validated, then proactively refreshed to capture a fresh token + expiry.

    Returns a dict with access_token / refresh_token / token_expires_at /
    subscriptions, plus ``code_ok`` / ``refresh_ok`` / ``refresh_error`` flags.
    """
    auth_code = _extract_auth_code(raw)
    client = TownGasApiClient(
        async_get_clientsession(hass),
        base_url,
        TokenStore("", None),
    )

    access = refresh = None
    expires_at = 0.0
    used_code = False
    if auth_code:
        used_code = await client.async_exchange_auth_code(auth_code)
        if used_code:
            access = client.tokens.access_token
            refresh = client.tokens.refresh_token
            expires_at = client.tokens.expires_at
        else:
            raise TownGasAuthError(
                "用微信登录码换发 token 失败："
                + (client.tokens.last_refresh_error or "未知原因")
            )

    if not used_code:
        try:
            access, refresh = _parse_token_json(raw)
        except ValueError:
            raise
        refresh_ok = await client.async_try_refresh_token()
        if refresh_ok:
            access = client.tokens.access_token
            refresh = client.tokens.refresh_token
            expires_at = client.tokens.expires_at

    # 用（换发后的）access_token 拉户号列表校验；失效则抛 TownGasAuthError。
    subs = await client.async_validate_token()
    return {
        CONF_ACCESS_TOKEN: access,
        CONF_REFRESH_TOKEN: refresh,
        CONF_TOKEN_EXPIRES_AT: expires_at,
        CONF_SUBSCRIPTIONS: subs,
        "code_ok": used_code,
        "refresh_ok": (not used_code)
        and client.tokens.last_refresh_error is None
        and bool(refresh),
        "refresh_error": client.tokens.last_refresh_error,
    }


class TownGasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Towngas."""

    VERSION = 1

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0
        self._base_url: str = DEFAULT_BASE_URL
        self._subs: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {
            "base_url": self._base_url or DEFAULT_BASE_URL,
            "wechat_login_url": wechat_login_url(),
            "detail": "",
        }
        if user_input is not None:
            base_url = (
                user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL
            ).rstrip("/")
            try:
                finalized = await _finalize_tokens(
                    self.hass, base_url, user_input[CONF_ACCESS_TOKEN]
                )
            except ValueError:
                errors[CONF_ACCESS_TOKEN] = "invalid_token_format"
            except TownGasAuthError as err:
                errors[CONF_ACCESS_TOKEN] = "invalid_auth"
                description_placeholders["detail"] = _format_detail(err)
                description_placeholders["base_url"] = base_url
            except TownGasApiError as err:
                errors["base"] = "cannot_connect"
                description_placeholders["detail"] = _format_detail(err)
                description_placeholders["base_url"] = base_url
            else:
                self._access_token = finalized[CONF_ACCESS_TOKEN]
                self._refresh_token = finalized[CONF_REFRESH_TOKEN]
                self._token_expires_at = finalized[CONF_TOKEN_EXPIRES_AT]
                self._base_url = base_url
                self._subs = finalized[CONF_SUBSCRIPTIONS]
                if (
                    not finalized.get("code_ok")
                    and finalized.get("refresh_ok") is False
                ):
                    _async_warn_refresh_unusable(
                        self.hass, finalized.get("refresh_error")
                    )
                if not self._subs:
                    errors["base"] = "no_subscriptions"
                else:
                    return await self.async_step_subs()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_subs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        options = [
            SelectOptionDict(
                value=f"{s.get('orgCode')}|{s.get('subsCode')}",
                label=(
                    f"{s.get('subsCode')} "
                    f"{s.get('name') or s.get('displayAddr') or ''}".strip()
                ),
            )
            for s in self._subs
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SUBSCRIPTIONS, default=[o["value"] for o in options]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        if user_input is not None:
            selected = user_input.get(CONF_SUBSCRIPTIONS) or []
            if not selected:
                errors[CONF_SUBSCRIPTIONS] = "no_subscriptions_selected"
            else:
                subs = []
                for sel in selected:
                    org_code, subs_code = sel.split("|", 1)
                    subs.append({"org_code": org_code, "subs_code": subs_code})
                return self.async_create_entry(
                    title="港华燃气",
                    data={
                        CONF_ACCESS_TOKEN: self._access_token,
                        CONF_REFRESH_TOKEN: self._refresh_token,
                        CONF_TOKEN_EXPIRES_AT: self._token_expires_at,
                        CONF_BASE_URL: self._base_url,
                        CONF_SUBSCRIPTIONS: subs,
                    },
                )

        return self.async_show_form(
            step_id="subs", data_schema=schema, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Perform reauth when the token expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for a fresh WeChat login code (or token JSON)."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {
            "wechat_login_url": wechat_login_url(),
            "detail": "",
        }
        entry = self._get_reauth_entry()

        if user_input is not None:
            base_url = entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            try:
                finalized = await _finalize_tokens(
                    self.hass, base_url, user_input[CONF_ACCESS_TOKEN]
                )
            except ValueError:
                errors[CONF_ACCESS_TOKEN] = "invalid_token_format"
            except TownGasAuthError as err:
                errors[CONF_ACCESS_TOKEN] = "invalid_auth"
                description_placeholders["detail"] = _format_detail(err)
            except TownGasApiError as err:
                errors["base"] = "cannot_connect"
                description_placeholders["detail"] = _format_detail(err)
            else:
                new_data = dict(entry.data)
                new_data[CONF_ACCESS_TOKEN] = finalized[CONF_ACCESS_TOKEN]
                new_data[CONF_REFRESH_TOKEN] = finalized[CONF_REFRESH_TOKEN]
                new_data[CONF_TOKEN_EXPIRES_AT] = finalized[CONF_TOKEN_EXPIRES_AT]
                if (
                    not finalized.get("code_ok")
                    and finalized.get("refresh_ok") is False
                ):
                    _async_warn_refresh_unusable(
                        self.hass, finalized.get("refresh_error")
                    )
                return self.async_update_reload_and_abort(entry, data=new_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_ACCESS_TOKEN): str}),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TownGasOptionsFlow:
        return TownGasOptionsFlow(config_entry)


class TownGasOptionsFlow(config_entries.OptionsFlow):
    """Options flow: update scan / token-refresh intervals."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            scan = user_input.get(OPT_SCAN_INTERVAL)
            token = user_input.get(OPT_TOKEN_REFRESH_INTERVAL)
            try:
                scan = int(scan)
                token = int(token)
            except (TypeError, ValueError):
                errors[OPT_SCAN_INTERVAL] = "invalid_number"
            else:
                if not (SCAN_INTERVAL_MIN <= scan <= SCAN_INTERVAL_MAX):
                    errors[OPT_SCAN_INTERVAL] = "scan_out_of_range"
                if not (
                    TOKEN_REFRESH_INTERVAL_MIN
                    <= token
                    <= TOKEN_REFRESH_INTERVAL_MAX
                ):
                    errors[OPT_TOKEN_REFRESH_INTERVAL] = "token_out_of_range"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        current_scan = self.config_entry.options.get(
            OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_token = self.config_entry.options.get(
            OPT_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_SCAN_INTERVAL, default=current_scan
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=SCAN_INTERVAL_MIN, max=SCAN_INTERVAL_MAX),
                ),
                vol.Required(
                    OPT_TOKEN_REFRESH_INTERVAL, default=current_token
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=TOKEN_REFRESH_INTERVAL_MIN,
                        max=TOKEN_REFRESH_INTERVAL_MAX,
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
