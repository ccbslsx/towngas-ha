#!/usr/bin/env python3
"""Probe the Towngas WeChat-central VCC gateway to discover real field names.

Usage:
    python tools/probe_vcc.py "<authCode>"

where <authCode> is the `code` from the WeChat login redirect URL
(one-time, valid a few minutes). The script:

  1. exchanges authCode -> access_token (+ refresh_token) via
     POST /vcc-oauth/oauth/authorize2/accessToekn?authCode=
  2. calls a set of /nv1/vcc-cbs endpoints with Bearer + sign, and prints
     the raw response structure (top-level keys, and the field names of the
     first `datas` record) so the integration's field mapping can be finalized.

Pure stdlib (urllib / ssl / hashlib / json) — no Home Assistant needed.
"""

import hashlib
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request

HOST = "weixin.towngasvcc.com"
CLIENT_ID = "pe92a8wechatMA0105"
OAUTH_PATH = "/vcc-oauth"
API_PATH = "/nv1/vcc-cbs"
SIGN_SALT = "hbasesoft.com-prod"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MicroMessenger/7.0.20"
)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def _sign(params: dict) -> str:
    keys = sorted(k for k, v in params.items()
                  if k != "sign" and v not in (None, ""))
    raw = "".join(f"{k}{params[k]}" for k in keys)
    return hashlib.md5((raw + SIGN_SALT).encode()).hexdigest().upper()


def _req(method: str, url: str, data=None):
    r = urllib.request.Request(url, data=data, headers={"User-Agent": UA}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=25, context=CTX) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _trunc(s: str, n=300) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def main():
    if len(sys.argv) < 2:
        print("用法: python probe_vcc.py <authCode>")
        sys.exit(2)
    auth_code = sys.argv[1].strip()

    # 1) exchange authCode -> token
    url = f"https://{HOST}{OAUTH_PATH}/oauth/authorize2/accessToekn?authCode={urllib.parse.quote(auth_code, safe='')}"
    st, body = _req("POST", url)
    print(f"[exchange] HTTP {st}  {_trunc(body)}")
    try:
        tok = json.loads(body)
    except json.JSONDecodeError:
        print("!! 换发失败，无法继续"); sys.exit(1)
    access = tok.get("access_token")
    if not access:
        print("!! 未拿到 access_token，终止"); sys.exit(1)
    print(f"[exchange] access_token={access[:12]}… expires_in={tok.get('expires_in')} refresh_token?={'Y' if tok.get('refresh_token') else 'N'}")

    def cbs(path: str, params: dict | None = None):
        ts = int(time.time() * 1000)
        p = {**(params or {}), "timestamp": ts}
        p["sign"] = _sign(p)
        u = f"https://{HOST}{API_PATH}{path}?{urllib.parse.urlencode(p)}"
        req = urllib.request.Request(
            u, headers={"User-Agent": UA, "Authorization": f"Bearer {access}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def show(path: str, params=None):
        st, b = cbs(path, params)
        print(f"\n=== {path}  (params={params})  HTTP {st} ===")
        try:
            d = json.loads(b)
        except json.JSONDecodeError:
            print("  body:", _trunc(b, 400)); return None
        print("  top-level keys:", list(d.keys()) if isinstance(d, dict) else type(d).__name__)
        if isinstance(d, dict):
            rc = d.get("resultCode")
            if rc is not None:
                print(f"  resultCode={rc} resultMsg={d.get('resultMsg')}")
            datas = d.get("datas")
            if isinstance(datas, list) and datas:
                rec = datas[0]
                if isinstance(rec, dict):
                    print(f"  datas[{len(datas)}]: sample keys = {list(rec.keys())}")
                    # 打印样本值（截断）
                    sample = {k: _trunc(str(v), 60) for k, v in rec.items()}
                    print("  sample values:", json.dumps(sample, ensure_ascii=False))
            elif isinstance(datas, dict):
                print("  datas(dict) keys =", list(datas.keys()))
        return d

    # 2) 户号列表
    subs = show("/usersubs/queryBindList")
    first = None
    if isinstance(subs, dict):
        lst = subs.get("datas") or []
        if lst:
            first = lst[0]
            print("  >> 首户号:", json.dumps({k: first.get(k) for k in ("subsCode", "orgCode", "name", "displayAddr") if k in first}, ensure_ascii=False))

    if first:
        sc = first.get("subsCode") or first.get("subsId")
        oc = first.get("orgCode") or first.get("orgId")
        # 3) 历史账单
        show("/charge/queryHistoryFee", {"subsCode": sc, "orgCode": oc, "pageIndex": 1, "pageSize": 3})
        # 4) 余额
        show("/charge/gasFeeBaseinfo", {"subsCode": sc, "orgCode": oc})
        # 5) 本期表数
        show("/charge/preCheck", {"subsCode": sc, "orgCode": oc})
        # 6) 户详情
        show("/usersubs/subsDetailByCode", {"subsCode": sc, "orgCode": oc})
    # 7) 登录用户信息
    show("/usersubs/getLoginUserInfo")


if __name__ == "__main__":
    main()
