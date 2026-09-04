"""Constants for the Towngas (港华燃气) integration."""

from __future__ import annotations

import logging
from typing import Final

# 集成版本（与 manifest.json 保持一致；仅用于启动日志，方便确认 HA 里实际跑的是哪版）。
VERSION: Final = "1.5.2"

DOMAIN: Final = "towngas"
LOGGER: Final = logging.getLogger(__package__)

# Config keys
CONF_BASE_URL: Final = "base_url"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_SUBSCRIPTIONS: Final = "subscriptions"
# 户号信息（v1.5.0 起手动填写，因为微信中央网关的 queryBindList 需要服务端才
# 认得的 org 参数，无法在集成内自动发现；参考杭州版「用户手动填 subs_id」模型）。
# - subs_id：preCheck 读数接口用的户号标识（杭州版实测字段名 subsId，最稳）。
# - subs_code / org_code：历史账单 / 余额接口（queryHistoryFee / gasFeeBaseinfo）
#   需要，选填；不填则账单类传感器留空（best-effort，待 authCode 探测校准）。
CONF_SUBS_ID: Final = "subs_id"
CONF_SUBS_CODE: Final = "subs_code"
CONF_ORG_CODE: Final = "org_code"
# 持久化的 token 过期时间戳（epoch 秒）。0 = 未知（退化为被动刷新）。
CONF_TOKEN_EXPIRES_AT: Final = "token_expires_at"

# Options
OPT_SCAN_INTERVAL: Final = "scan_interval"          # 数据刷新间隔（秒）
DEFAULT_SCAN_INTERVAL: Final = 21600                # 默认 6 小时
OPT_TOKEN_REFRESH_INTERVAL: Final = "token_refresh_interval"  # Token 健康检查间隔（秒）
# Token 健康检查间隔（秒）。v1.5.0 改用微信中央 OAuth：access_token 寿命 7200 秒，
# 默认 1800 秒（30 分钟）刷新一次，留足余量。取值范围放宽到 7200（不超过 token 寿命）。
DEFAULT_TOKEN_REFRESH_INTERVAL: Final = 1800

# scan_interval 允许取值范围（秒）
SCAN_INTERVAL_MIN: Final = 60
SCAN_INTERVAL_MAX: Final = 86400
# token_refresh_interval 允许取值范围（秒）
TOKEN_REFRESH_INTERVAL_MIN: Final = 300
TOKEN_REFRESH_INTERVAL_MAX: Final = 7200

# 港华燃气系统每日维护窗口（CST = UTC+8）。窗口内跳过数据请求，传感器保持上次读数；
# Token 健康检查不受维护窗口影响。
MAINTENANCE_START_HOUR: Final = 23
MAINTENANCE_START_MINUTE: Final = 30
MAINTENANCE_END_HOUR: Final = 0
MAINTENANCE_END_MINUTE: Final = 30

# ============================================================================
# v1.5.0：微信中央 VCC 网关（解决营业厅 token 每几天必过期的问题）
# ----------------------------------------------------------------------------
# 营业厅网关（maanshan.towngasvcc.com/openapi/uv1）的 access_token 仅 899 秒、
# refresh_token 天级就失效 → 用户每隔几天就要重粘 tokenCode。
# 微信中央网关（weixin.towngasvcc.com）的 access_token 7200 秒、refresh_token 天级
# 且用户实测稳定 1 周+，鉴权层整体切换后即可做到「登录一次、长期免手动」。
# 注意：两套网关的 token 不互通（营业厅网关拒收微信 token，返回 20001），
# 因此数据层也必须随之切到 /nv1/vcc-cbs。
# ============================================================================
WECHAT_HOST: Final = "weixin.towngasvcc.com"
DEFAULT_BASE_URL: Final = f"https://{WECHAT_HOST}"

# 微信中央 OAuth 的 client_id。马鞍山专属 client_id 为 pe92a8wechatMA0105
#（杭州为 pe92a8wechatYH0105，中央 OAuth 跨城市通用，但用马鞍山自身 client 更稳）。
WECHAT_CLIENT_ID: Final = "pe92a8wechatMA0105"
WECHAT_APPID: Final = "wxc4be7dee36d3b4a2"
WECHAT_OAUTH_PATH: Final = "/vcc-oauth"
WECHAT_API_PATH: Final = "/nv1/vcc-cbs"
# 微信中央网关请求签名盐值（杭州版逆向得到）。
WECHAT_SIGN_SALT: Final = "hbasesoft.com-prod"
# union 登录成功后的回跳地址（仅用于拼登录链接，服务端不严格比对）。
WECHAT_REDIRECT_URI: Final = f"https://{WECHAT_HOST}/h5-gas/"

# 微信 OAuth token 寿命（秒）。实测 access_token=7200，refresh_token 天级。
DEFAULT_TOKEN_EXPIRES_IN: Final = 7200
# 剩余寿命低于此秒数即主动刷新。取 60 秒（杭州版做法），7200s 寿命下极宽裕。
# 注意：仅用于「已知过期时间」的即时判断；保活定时器用的是动态 buffer
# （见 coordinator._refresh_buffer），保证刷新检查点不会落在 token 已过期之后。
TOKEN_EXPIRY_BUFFER_SECS: Final = 60
# 保活缓冲余量（秒）：在「健康检查间隔」之上额外预留的提前量。
# 若只留 60s 而检查间隔是 1800s，检查点可能落在 token 过期之后（例如
# T+7199 才检查，或 T+7300 才检查——此时 token 已死 100 秒），导致必须先
# 挨一次鉴权失败才能恢复。留 120s 余量可确保刷新检查点永远落在有效期内。
TOKEN_REFRESH_SAFETY_MARGIN_SECS: Final = 120
# 连续刷新失败达到此次数才触发 reauth。
# 目的：抗网络抖动/服务端临时错误——单次失败就 reauth 会把集成打死，
# 而实际上很多失败下一个周期就自愈了。默认 3 次（配合 1800s 间隔 ≈ 1.5 小时容错窗口）。
TOKEN_REFRESH_FAILURE_THRESHOLD: Final = 3

# ---------------------------------------------------------------------------
# 模块级共享状态（进程内，重启即清空）
# ---------------------------------------------------------------------------
# 正在把 token 写回 config entry 的 entry_id 集合。
# 背景（v1.5.2 修复的严重 bug）：_persist_tokens() 调用
# async_update_entry(data=...) 会触发 entry 的 update listener，而旧版 listener
# 无条件执行 async_reload() → **每刷新一次 token 就重载一次整个集成**。
# 后果有两层：
#   1. 表现层：4 个 token 实体周期性变成 unavailable（重载瞬间实体下线），
#      历史曲线呈现「正常 / 不可用 / 正常 / 不可用」的锯齿。
#   2. 致命层：某次重载若恰好赶上鉴权边缘状态（网络抖动、服务端临时错误），
#      async_setup_entry 的首次刷新就会抛 ConfigEntryAuthFailed → 集成进入
#      reauth 状态 → 保活定时器随卸载一起消失 → token 再也没人续期 → 彻底过期。
# 修复：写回 token 前登记 entry_id，listener 命中即跳过本次 reload。
TOKEN_PERSIST_IN_PROGRESS: set = set()

USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090c33) XWEB/14315 Flue"
)

# 调试/手动服务：强制刷新 token（用于验证刷新机制是否工作）
SERVICE_FORCE_REFRESH: Final = "force_refresh_token"
# 调试/手动服务：导出各接口原始返回，便于在不暴露 authCode 的情况下校准字段
# 名与金额单位（用户在自己 HA 里运行，把返回贴回开发者确认即可）。
SERVICE_DUMP_RAW: Final = "dump_raw"

# ----------------------------------------------------------------------------
# 以下为 v1.4.0 及之前的「营业厅网关」常量，v1.5.0 已不再使用，仅保留以避免
# 历史配置/导入引用报错。新代码一律走上面的 WECHAT_* 常量。
# ----------------------------------------------------------------------------
# 营业厅 web client_id（已弃用）
DEPRECATED_BUSINESS_CLIENT_ID: Final = "db196d62f7d211e8a9b2fa163e955d28"
# 营业厅 OAuth / 授权码端点（已弃用）
DEPRECATED_OAUTH_TOKEN_PATH: Final = "/openapi/uv1/oauth/token"
DEPRECATED_OAUTH_CODE2TOKEN_PATH: Final = "/openapi/uv1/weboauth2Code2Token"
DEPRECATED_BUSINESS_HALL_URL: Final = "https://maanshan.towngasvcc.com/h5-gas/"
