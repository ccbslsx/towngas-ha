"""Constants for the Towngas (港华燃气) integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "towngas"
LOGGER: Final = logging.getLogger(__package__)

# Config keys
CONF_BASE_URL: Final = "base_url"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_SUBSCRIPTIONS: Final = "subscriptions"

# Options
OPT_SCAN_INTERVAL: Final = "scan_interval"          # 数据刷新间隔（秒）
DEFAULT_SCAN_INTERVAL: Final = 21600                # 默认 6 小时
OPT_TOKEN_REFRESH_INTERVAL: Final = "token_refresh_interval"  # Token 健康检查间隔（秒）
DEFAULT_TOKEN_REFRESH_INTERVAL: Final = 1800        # 默认 30 分钟

# scan_interval 允许取值范围（秒）
SCAN_INTERVAL_MIN: Final = 60
SCAN_INTERVAL_MAX: Final = 86400
# token_refresh_interval 允许取值范围（秒）
TOKEN_REFRESH_INTERVAL_MIN: Final = 300
TOKEN_REFRESH_INTERVAL_MAX: Final = 7140

# 港华燃气系统每日维护窗口（CST = UTC+8）。窗口内跳过数据请求，传感器保持上次读数；
# Token 健康检查不受维护窗口影响。
MAINTENANCE_START_HOUR: Final = 23
MAINTENANCE_START_MINUTE: Final = 30
MAINTENANCE_END_HOUR: Final = 0
MAINTENANCE_END_MINUTE: Final = 30

DEFAULT_BASE_URL: Final = "https://maanshan.towngasvcc.com"

# The web client_id used by the 网上营业厅 frontend
CLIENT_ID: Final = "db196d62f7d211e8a9b2fa163e955d28"

# resultCode values that mean "token invalid / expired"
AUTH_ERROR_CODES: Final = {"20001", "40058"}
