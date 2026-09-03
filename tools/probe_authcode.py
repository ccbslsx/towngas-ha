import urllib.request, urllib.parse, ssl, json

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
CID = "db196d62f7d211e8a9b2fa163e955d28"  # 马鞍山 client_id
UA = {"User-Agent": "Mozilla/5.0"}


def req(method, url, data=None):
    r = urllib.request.Request(url, data=data, headers=UA, method=method)
    try:
        with urllib.request.urlopen(r, timeout=25, context=ctx) as resp:
            return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa
        return -1, {}, f"{type(e).__name__}: {e}"


print("=== A. accessToekn?authCode=FAKE (POST) — 杭州换码端点 ===")
s, h, b = req("POST", "https://weixin.towngasvcc.com/vcc-oauth/oauth/authorize2/accessToekn?authCode=FAKECODE123")
print("  HTTP", s, "| body=", b)

print("=== B. accessToekn?authCode=FAKE (GET) ===")
s, h, b = req("GET", "https://weixin.towngasvcc.com/vcc-oauth/oauth/authorize2/accessToekn?authCode=FAKECODE123")
print("  HTTP", s, "| body=", b)

print("=== C. union 登录链接（马鞍山 client_id） ===")
ru = urllib.parse.quote("https://maanshan.towngasvcc.com/h5-gas/", safe="")
u = f"https://weixin.towngasvcc.com/vcc-oauth/oauth/authorize2/union?clientid={CID}&redirectUri={ru}"
s, h, b = req("GET", u)
print("  HTTP", s, "| Location=", h.get("Location", "")[:300])
print("  | body=", b[:200])
