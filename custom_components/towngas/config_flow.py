"""Config flow for the Towngas (港华燃气) integration."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
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
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): str,
        vol.Optional(
            CONF_BASE_URL, description={"suggested_value": DEFAULT_BASE_URL}
        ): str,
    }
)


def _parse_token_input(raw: str) -> tuple[str, str | None]:
    """Accept either a bare access_token or the full localStorage JSON."""
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
        # Maybe the JSON got mangled by copy/paste - try to regex the token out
        m = re.search(r'"access_token"\s*:\s*"([^"]+)"', raw)
        if m:
            r = re.search(r'"refresh_token"\s*:\s*"([^"]+)"', raw)
            return m.group(1), r.group(1) if r else None
        raise ValueError("无法从输入中解析 access_token")
    return raw, None


async def _validate(
    hass, base_url: str, access: str, refresh: str | None
) -> list[dict[str, Any]]:
    """Validate the token; returns bound subs. Raises on failure."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = TownGasApiClient(
        async_get_clientsession(hass),
        base_url,
        TokenStore(access, refresh),
    )
    return await client.async_validate_token()


class TownGasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Towngas."""

    VERSION = 1

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._base_url: str = DEFAULT_BASE_URL
        self._subs: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        if user_input is not None:
            base_url = (
                user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL
            ).rstrip("/")
            try:
                access, refresh = _parse_token_input(
                    user_input[CONF_ACCESS_TOKEN]
                )
            except ValueError:
                errors[CONF_ACCESS_TOKEN] = "invalid_token_format"
            else:
                try:
                    subs = await _validate(self.hass, base_url, access, refresh)
                except TownGasAuthError as err:
                    errors[CONF_ACCESS_TOKEN] = "invalid_auth"
                    description_placeholders["detail"] = str(err)
                except TownGasApiError as err:
                    errors["base"] = "cannot_connect"
                    description_placeholders["detail"] = str(err)
                else:
                    self._access_token = access
                    self._refresh_token = refresh
                    self._base_url = base_url
                    self._subs = subs
                    if not subs:
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
                    subs.append({"orgCode": org_code, "subsCode": subs_code})
                return self.async_create_entry(
                    title="港华燃气",
                    data={
                        CONF_ACCESS_TOKEN: self._access_token,
                        CONF_REFRESH_TOKEN: self._refresh_token,
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
        """Ask for a fresh token."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            base_url = entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            try:
                access, refresh = _parse_token_input(
                    user_input[CONF_ACCESS_TOKEN]
                )
            except ValueError:
                errors[CONF_ACCESS_TOKEN] = "invalid_token_format"
            else:
                try:
                    await _validate(self.hass, base_url, access, refresh)
                except TownGasAuthError as err:
                    errors[CONF_ACCESS_TOKEN] = "invalid_auth"
                    description_placeholders["detail"] = str(err)
                except TownGasApiError as err:
                    errors["base"] = "cannot_connect"
                    description_placeholders["detail"] = str(err)
                else:
                    new_data = dict(entry.data)
                    new_data[CONF_ACCESS_TOKEN] = access
                    new_data[CONF_REFRESH_TOKEN] = refresh
                    return self.async_update_reload_and_abort(
                        entry, data=new_data
                    )

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
    """Options flow: update / token-refresh intervals."""

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
