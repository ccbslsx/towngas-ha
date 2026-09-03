"""Constants for the Towngas (港华燃气) integration."""

from __future__ import annotations

import logging
from typing import Final

# 集成版本（与 manifest.json 保持一致；仅用于启动日志，方便确认 HA 里实际跑的是哪版）。
VERSION: Final = "1.4.0"

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
# Token 健康检查间隔（秒）。实测 access_token 寿命仅 899 秒，
# 默认取 900 秒使其同时充当保活与失效探测（取值须落在 TOKEN_REFRESH_INTERVAL_* 之间）。
DEFAULT_TOKEN_REFRESH_INTERVAL: Final = 600

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

# --- Token 刷新（城市级标准 OAuth2，与业务接口同 host） ---
# 逆向自营业厅前端 JS（/js/app_*.js）：登录走 login.towngasvcc.com/oauth/authorize，
# 而 code/refresh_token 换发走**业务 host 自己**的 /openapi/uv1/oauth/token。
# 之前误用 weixin.towngasvcc.com/vcc-oauth（微信小程序那套 oauth），
# 它与营业厅 client_id 不互通，刷新恒定返回 90143 → 这就是反复 reauth 的根因。
OAUTH_TOKEN_PATH: Final = "/openapi/uv1/oauth/token"
# 标准 OAuth2 参数（前端 getToken 里写死的取值）
OAUTH_GRANT_TYPE_REFRESH: Final = "refresh_token"
OAUTH_SCOPE: Final = "read write"
# redirect_uri 只做非空校验，服务端不比对；用营业厅首页即可。
OAUTH_REDIRECT_URI_PATH: Final = "/h5-gas/"
# 实测：马鞍山 access_token 有效期仅 899 秒（约 15 分钟），且 refresh_token 不轮换
# （可永久复用）。因此必须高频主动续期，且每次请求前都要保证 token 新鲜。
DEFAULT_TOKEN_EXPIRES_IN: Final = 899
# 剩余寿命低于此秒数即主动刷新。取 300 秒（15 分钟寿命的 1/3），
# 保证即使网络抖动重试也来得及。
TOKEN_EXPIRY_BUFFER_SECS: Final = 300

# 接口码（前端 ajax 里的 code 字段）
CODE_OAUTH_TOKEN: Final = 1502

USER_AGENT: Final = "Mozilla/5.0"

# --- 授权码换 token（营业厅登录后浏览器重定向带回 tokenCode，用此端点换新 token） ---
# 逆向自营业厅前端 app.js：登录成功 → /loginRedirect?...&tokenCode=XXX →
# 调 weboauth2Code2Token?tokenCode=XXX 得到 access_token+refresh_token。
# 这样重认证只需「打开营业厅 → 登录 → 复制地址栏里 tokenCode=... 那段 → 粘贴」，
# 不再去 localStorage 挖 JSON，体验等同杭州港华的「扫码 → 粘 authCode」。
# 实测：传假 tokenCode 返回 resultCode=90142「授权码已失效」，说明端点接受该参数。
OAUTH_CODE2TOKEN_PATH: Final = "/openapi/uv1/weboauth2Code2Token"
OAUTH_CODE_PARAM: Final = "tokenCode"
BUSINESS_HALL_URL: Final = "https://maanshan.towngasvcc.com/h5-gas/"

# 调试/手动服务：强制刷新 token（用于验证刷新机制是否工作）
SERVICE_FORCE_REFRESH: Final = "force_refresh_token"
