"""Probe the Towngas token refresh endpoint end-to-end.

Validates exactly what custom_components/towngas/api.py does:

  GET {base}/openapi/uv1/oauth/token
      ?seq=<seq>&client_id=<cid>
      &grant_type=refresh_token&refresh_token=<rt>
      &scope=read write&redirect_uri=<base>/h5-gas/

Background (reverse engineered from the 营业厅 frontend JS):
  * The 营业厅 web client (client_id db196d62...) logs in via
    login.towngasvcc.com/oauth/authorize but exchanges code / refresh_token on
    the **business host itself** (/openapi/uv1/oauth/token) — a plain OAuth2
    endpoint.
  * weixin.towngasvcc.com/vcc-oauth is the WeChat mini-program oauth and is
    NOT compatible with this client_id: it always answers 90143
    "refreshToken已失效". Do not use it.
  * Measured on 马鞍山: access_token lives 899 s (~15 min) and refresh_token is
    NOT rotated, so a single pasted refresh_token works indefinitely.

Usage:
  python tools/probe_refresh.py --access <AT> --refresh <RT>
"""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "db196d62f7d211e8a9b2fa163e955d28"
CODE_OAUTH_TOKEN = 1502
CODE_QUERY_BIND_SUBS = 3529
OAUTH_TOKEN_PATH = "/openapi/uv1/oauth/token"
OAUTH_SCOPE = "read write"
DEFAULT_BASE_URL = "https://maanshan.towngasvcc.com"
UA = "Mozilla/5.0"


def _seq(code: int) -> str:
    return f"{code:05d}" + time.strftime("%Y%m%d%H%M%S") + "1234567890123"


def _http(url: str, method: str = "GET") -> tuple[int, str]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--access", required=True)
    ap.add_argument("--refresh", required=True)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    a = ap.parse_args()
    base = a.base_url.rstrip("/")

    print("STEP 1 — 校验 access_token 对业务接口是否有效")
    url = (
        f"{base}/openapi/uv1/user/queryBindSubsLimitServer"
        f"?seq={_seq(CODE_QUERY_BIND_SUBS)}&token={a.access}"
        f"&client_id={CLIENT_ID}&isPayOrReport=Y"
    )
    s, b = _http(url)
    ok_biz = "resultCode" not in b
    print(f"  HTTP {s} -> {'有效' if ok_biz else '无效'}  {b[:160]}")

    print("\nSTEP 2 — 用 refresh_token 换新 access_token")
    params = {
        "seq": _seq(CODE_OAUTH_TOKEN),
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": a.refresh,
        "scope": OAUTH_SCOPE,
        "redirect_uri": f"{base}/h5-gas/",
    }
    url = f"{base}{OAUTH_TOKEN_PATH}?{urllib.parse.urlencode(params)}"
    s, b = _http(url)
    print(f"  HTTP {s} -> {b[:300]}")

    new_at = None
    if '"access_token"' in b:
        import json

        d = json.loads(b)
        new_at = d.get("access_token")
        print(f"\n  刷新成功: expires_in={d.get('expires_in')} 秒")
        print(f"  refresh_token {'未轮换（可永久复用）' if d.get('refresh_token') == a.refresh else '已轮换：需持久化新值'}")
    else:
        print("\n  刷新失败 —— refresh_token 已作废，需要重新粘贴一份")
        return 1

    print("\nSTEP 3 — 用新 access_token 再打一次业务接口")
    url = (
        f"{base}/openapi/uv1/user/queryBindSubsLimitServer"
        f"?seq={_seq(CODE_QUERY_BIND_SUBS)}&token={new_at}"
        f"&client_id={CLIENT_ID}&isPayOrReport=Y"
    )
    s, b = _http(url)
    print(f"  HTTP {s} -> {'新 token 可用' if 'resultCode' not in b else '新 token 被拒'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
