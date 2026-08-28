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
# 持久化的 token 过期时间戳（epoch 秒）。0 = 未知（退化为被动刷新）。
CONF_TOKEN_EXPIRES_AT: Final = "token_expires_at"

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

# --- Token 刷新（平台级 oauth，城市间通用） ---
# 刷新端点位于共享 oauth 服务（与杭州/马鞍山业务 host 解耦）。
OAUTH_REFRESH_URL: Final = (
    "https://weixin.towngasvcc.com/vcc-oauth/oauth/authorize2/refreshToken"
)
# 签名盐：MD5(排序的 key+value 拼接 + 本盐) 后转大写。平台级，城市通用。
SIGN_SALT: Final = "hbasesoft.com-prod"
# 当 expires_in 缺失时的兜底值（秒）。
DEFAULT_TOKEN_EXPIRES_IN: Final = 7200
# 在 token 真正过期前多少秒提前刷新，避免临界窗口内请求失败。
TOKEN_EXPIRY_BUFFER_SECS: Final = 120

USER_AGENT: Final = "Mozilla/5.0"

# 调试/手动服务：强制刷新 token（用于验证刷新机制是否工作）
SERVICE_FORCE_REFRESH: Final = "force_refresh_token"
