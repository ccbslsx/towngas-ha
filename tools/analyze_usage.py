#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""港华燃气（马鞍山）用气统计分析脚本 —— 独立运行，无需 Home Assistant。

从港华燃气 VCC 网上营业厅 openapi 拉取「历史账单」，计算常用统计指标并输出：
  * 月度明细（用气量 m³ / 金额 元 / 实际单价 元·m³）
  * 年度汇总（总用量、总金额、月数、月均用量、月均金额）
  * 同比（YoY）：相邻两年全年对比 + 增长率
  * 指定月份逐年对比（如每年 2 月）
  * 实际单位气价趋势
  * 季节性：采暖季（12/1/2 月）vs 非采暖季用量与金额

输出：
  * <out>/towngas_usage.csv   原始账单明细
  * <out>/towngas_report.html 自包含可视化报告（Chart.js CDN）

零第三方依赖，仅用 Python 标准库。Python 3.8+ 可运行。

用法：
  python analyze_usage.py --base-url https://maanshan.towngasvcc.com \
      --token <ACCESS_TOKEN> [--refresh-token <REFRESH>] \
      [--subs-code XXX --org-code YYY] [--out ./out]

如果不传 --subs-code/--org-code，脚本会先调用 queryBindSubs 取第一个绑定户号。
token 过期且提供了 --refresh-token 时，会自动走平台级 oauth 刷新后重试。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# 接口常量（与 custom_components/towngas/api.py 保持一致）
# ---------------------------------------------------------------------------
CLIENT_ID = "db196d62f7d211e8a9b2fa163e955d28"  # 与集成 const.py 保持一致
CODE_QUERY_BIND_SUBS = 3529
CODE_QUERY_BILLS = 3516
# 刷新端点：城市业务 host 上的标准 OAuth2（与集成 const.py 一致）。
# ⚠️ 不要用 weixin.towngasvcc.com/vcc-oauth —— 那是微信小程序那套 oauth，
#    与营业厅 client_id 不互通，刷新恒定返回 90143。
OAUTH_TOKEN_PATH = "/openapi/uv1/oauth/token"
CODE_OAUTH_TOKEN = 1502
OAUTH_SCOPE = "read write"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PAGE_SIZE = 100
HEATING_MONTHS = {12, 1, 2}   # 采暖季（粗略：冬季用气高）


def _build_seq(code: int) -> str:
    return (
        f"{code:05d}"
        + datetime.now().strftime("%Y%m%d%H%M%S")
        + f"{random.randint(0, 10 ** 13 - 1):013d}"
    )


def _sign(params: dict[str, Any]) -> str:
    import hashlib
    keys = sorted(
        k for k, v in params.items()
        if k != "sign" and v is not None and v != ""
    )
    raw = "".join(f"{k}{params[k]}" for k in keys)
    return hashlib.md5((raw + SIGN_SALT).encode()).hexdigest().upper()


def _http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "ignore")
    return json.loads(body)


def _http_post_json(url: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"User-Agent": USER_AGENT,
                                     "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


# ---------------------------------------------------------------------------
# API 封装
# ---------------------------------------------------------------------------
class TownGasStatsClient:
    def __init__(self, base_url: str, token: str, refresh_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.refresh_token = refresh_token

    def _bill_url(self, subs_code: str, org_code: str,
                  page_index: int) -> str:
        path = "/openapi/uv1/bill/queryBills"
        url = f"{self.base_url}{path}"
        url += f"?seq={_build_seq(CODE_QUERY_BILLS)}"
        url += f"&token={self.token}"
        url += f"&client_id={CLIENT_ID}"
        url += f"&subsCode={subs_code}&orgCode={org_code}"
        url += f"&pageIndex={page_index}&pageSize={PAGE_SIZE}"
        return url

    def get_bound_subs(self) -> list[dict[str, Any]]:
        path = "/openapi/uv1/user/queryBindSubsLimitServer"
        for attempt in range(2):
            url = (f"{self.base_url}{path}?seq={_build_seq(CODE_QUERY_BIND_SUBS)}"
                   f"&token={self.token}&client_id={CLIENT_ID}&isPayOrReport=Y")
            data = _http_get_json(url)
            rc = data.get("resultCode")
            if rc is not None and str(rc) != "0":
                # token 过期 -> 刷新后重试一次
                if str(rc) in ("20001", "40058") and self.refresh_token and attempt == 0:
                    if self._try_refresh():
                        continue
                raise RuntimeError(
                    f"queryBindSubs 失败 resultCode={rc}: {data.get('resultMsg')}"
                )
            return data.get("datas") or []
        return []

    def ensure_token(self) -> None:
        """开局先续期一次。

        access_token 实测寿命仅约 15 分钟，而从营业厅复制 token 到运行脚本
        往往已过去更久，所以必须在拉取数据之前先换发一个新 token，
        否则第一次请求就会因 20001 失败。
        """
        if self.refresh_token:
            self._try_refresh()

    def get_bills(self, subs_code: str, org_code: str) -> list[dict[str, Any]]:
        """分页拉取全部历史账单，返回按账期升序排列的列表。"""
        all_bills: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                data = _http_get_json(self._bill_url(subs_code, org_code, page))
            except urllib.error.HTTPError as e:
                # token 过期 -> 尝试刷新后重试一次
                if e.code in (401, 403) and self.refresh_token:
                    if self._try_refresh():
                        continue
                raise
            rc = data.get("resultCode")
            if rc is not None and str(rc) != "0":
                if str(rc) in ("20001", "40058") and self.refresh_token:
                    if self._try_refresh():
                        continue
                raise RuntimeError(f"queryBills 失败 resultCode={rc}: {data.get('resultMsg')}")
            batch = data.get("datas") or []
            all_bills.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        all_bills.sort(key=lambda b: str(b.get("yrMonth", "")))
        return all_bills

    def _try_refresh(self) -> bool:
        """用 refresh_token 换新 access_token（城市级标准 OAuth2 端点）。"""
        if not self.refresh_token:
            return False
        params = {
            "seq": _build_seq(CODE_OAUTH_TOKEN),
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": OAUTH_SCOPE,
            "redirect_uri": f"{self.base_url}/h5-gas/",
        }
        url = f"{self.base_url}{OAUTH_TOKEN_PATH}?{urllib.parse.urlencode(params)}"
        try:
            data = _http_get_json(url)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] token 刷新请求失败: {e}", file=sys.stderr)
            return False
        new_access = (data or {}).get("access_token")
        if not new_access:
            rc = (data or {}).get("resultCode")
            print(
                f"[warn] token 刷新失败 resultCode={rc} "
                f"{ (data or {}).get('resultMsg', '') }",
                file=sys.stderr,
            )
            return False
        if (data or {}).get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        self.token = new_access
        print("[ok] access_token 已刷新，继续拉取", file=sys.stderr)
        return True


# ---------------------------------------------------------------------------
# 数据归一化
# ---------------------------------------------------------------------------
def _to_float(v: Any) -> float | None:
    try:
        if v in (None, ""):
            return None
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def normalize_bills(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把原始账单转成统一结构，chrgSum(分) -> 元。"""
    out: list[dict[str, Any]] = []
    for b in raw:
        ym = str(b.get("yrMonth") or "")
        if len(ym) != 6 or not ym.isdigit():
            continue
        usage = _to_float(b.get("amount"))
        cost_fen = _to_float(b.get("chrgSum"))
        cost = round(cost_fen / 100.0, 2) if cost_fen is not None else None
        steps = b.get("stepFeeResults") or []
        out.append({
            "yrMonth": ym,
            "year": int(ym[0:4]),
            "month": int(ym[4:6]),
            "usage_m3": usage,
            "cost_yuan": cost,
            "curr_reading": _to_float(b.get("currReading")),
            "last_reading": _to_float(b.get("lastReading")),
            "avg_price": round(cost / usage, 4) if (cost and usage) else None,
            "tiers": [
                {
                    "price": _to_float(s.get("price")),
                    "amount_m3": _to_float(s.get("amount")),
                    "cost_yuan": _to_float(s.get("chrgSum")),
                }
                for s in steps
            ],
        })
    return out


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def compute_monthly(bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return bills  # 已按账期升序，每条即一个月


def compute_yearly(bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    years: dict[int, list[dict[str, Any]]] = {}
    for b in bills:
        years.setdefault(b["year"], []).append(b)
    rows = []
    for y, items in sorted(years.items()):
        usages = [b["usage_m3"] for b in items if b["usage_m3"] is not None]
        costs = [b["cost_yuan"] for b in items if b["cost_yuan"] is not None]
        total_u = round(sum(usages), 2)
        total_c = round(sum(costs), 2)
        rows.append({
            "year": y,
            "months": len(items),
            "total_usage_m3": total_u,
            "total_cost_yuan": total_c,
            "avg_usage_m3": round(total_u / len(usages), 2) if usages else None,
            "avg_cost_yuan": round(total_c / len(costs), 2) if costs else None,
        })
    return rows


def compute_yoy(yearly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for i in range(1, len(yearly)):
        prev, cur = yearly[i - 1], yearly[i]
        du = _pct_change(cur["total_usage_m3"], prev["total_usage_m3"])
        dc = _pct_change(cur["total_cost_yuan"], prev["total_cost_yuan"])
        rows.append({
            "year": cur["year"],
            "prev_year": prev["year"],
            "usage_prev": prev["total_usage_m3"],
            "usage_cur": cur["total_usage_m3"],
            "usage_yoy": du,
            "cost_prev": prev["total_cost_yuan"],
            "cost_cur": cur["total_cost_yuan"],
            "cost_yoy": dc,
        })
    return rows


def compute_month_compare(bills: list[dict[str, Any]], month: int) -> list[dict[str, Any]]:
    rows = []
    for y in sorted({b["year"] for b in bills}):
        hit = next((b for b in bills if b["year"] == y and b["month"] == month), None)
        if hit:
            rows.append({
                "year": y,
                "usage_m3": hit["usage_m3"],
                "cost_yuan": hit["cost_yuan"],
                "avg_price": hit["avg_price"],
            })
    return rows


def compute_season(bills: list[dict[str, Any]]) -> dict[str, Any]:
    heat = [b for b in bills if b["month"] in HEATING_MONTHS]
    off = [b for b in bills if b["month"] not in HEATING_MONTHS]
    return {
        "heating": _season_agg(heat),
        "off": _season_agg(off),
    }


def _season_agg(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"count": 0, "total_usage_m3": 0, "total_cost_yuan": 0,
                "avg_usage_m3": None, "avg_cost_yuan": None}
    u = [b["usage_m3"] for b in items if b["usage_m3"] is not None]
    c = [b["cost_yuan"] for b in items if b["cost_yuan"] is not None]
    tu = round(sum(u), 2)
    tc = round(sum(c), 2)
    return {
        "count": len(items),
        "total_usage_m3": tu,
        "total_cost_yuan": tc,
        "avg_usage_m3": round(tu / len(u), 2) if u else None,
        "avg_cost_yuan": round(tc / len(c), 2) if c else None,
    }


def _pct_change(cur: float | None, prev: float | None) -> float | None:
    if cur is None or not prev:
        return None
    return round((cur - prev) / prev * 100.0, 1)


def build_sample_bills() -> list[dict[str, Any]]:
    """生成与真实 API 同构的示例账单（演示用，非真实数据）。

    模拟马鞍山家庭用气：采暖季(12/1/2月)用量显著升高，单价逐年微涨。
    """
    import random
    random.seed(20260827)
    bills: list[dict[str, Any]] = []
    cum = 12000.0
    for y in (2024, 2025, 2026):
        for m in range(1, 13):
            if y == 2026 and m > 7:
                break
            base = 34.0 if m in HEATING_MONTHS else 16.0
            usage = round(base + random.uniform(-4, 6), 1)
            price = round(2.98 + (y - 2024) * 0.12 + (0.05 if m in HEATING_MONTHS else 0.0), 2)
            cost_yuan = round(usage * price, 2)
            cum += usage
            bills.append({
                "yrMonth": f"{y}{m:02d}",
                "amount": usage,
                "chrgSum": int(round(cost_yuan * 100)),
                "currReading": round(cum, 2),
                "lastReading": round(cum - usage, 2),
                "stepFeeResults": [
                    {"price": price, "amount": usage,
                     "chrgSum": int(round(cost_yuan * 100))}
                ],
            })
    return bills


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def write_csv(bills: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["账期", "年", "月", "用气量_m3", "金额_元",
                    "实际单价_元每m3", "本期表数", "上期表数"])
        for b in bills:
            w.writerow([b["yrMonth"], b["year"], b["month"], b["usage_m3"],
                        b["cost_yuan"], b["avg_price"], b["curr_reading"],
                        b["last_reading"]])


def _money(v: float | None) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "-"


def _m3(v: float | None) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "-"


def _pct(v: float | None) -> str:
    return f"{v:+.1f}%" if isinstance(v, (int, float)) else "-"


def build_html(bills: list[dict[str, Any]], yearly: list[dict[str, Any]],
               yoy: list[dict[str, Any]], month_compare: list[dict[str, Any]],
               season: dict[str, Any], focus_month: int, note: str = "") -> str:
    monthly_labels = [b["yrMonth"] for b in bills]
    usage_series = [b["usage_m3"] or 0 for b in bills]
    cost_series = [b["cost_yuan"] or 0 for b in bills]
    price_series = [b["avg_price"] or 0 for b in bills]

    # 同比对比图：取最近两年逐月用量
    yoy_html = ""
    if yoy:
        last = yoy[-1]
        cmp_rows = "".join(
            f"<tr><td>{r['year']} 年</td><td>{_m3(r['usage_m3'])}</td>"
            f"<td>{_money(r['cost_yuan'])}</td><td>{_pct_change(r['avg_price'], None) if r['avg_price'] is None else _money(r['avg_price'])}</td></tr>"
            for r in month_compare
        )
        yoy_html = f"""
        <h2>{last['prev_year']} vs {last['year']} 全年同比</h2>
        <div class="cards">
          <div class="card"><div class="k">用量同比</div><div class="v">{_pct(last['usage_yoy'])}</div>
            <div class="s">{_m3(last['usage_prev'])} → {_m3(last['usage_cur'])} m³</div></div>
          <div class="card"><div class="k">金额同比</div><div class="v">{_pct(last['cost_yoy'])}</div>
            <div class="s">¥{_money(last['cost_prev'])} → ¥{_money(last['cost_cur'])}</div></div>
        </div>
        <h3>{focus_month} 月逐年对比</h3>
        <table><thead><tr><th>年份</th><th>用气量 (m³)</th><th>金额 (元)</th><th>实际单价</th></tr></thead>
        <tbody>{cmp_rows}</tbody></table>
        """

    season_html = f"""
      <h2>季节性（采暖季 {sorted(HEATING_MONTHS)} 月 vs 其余）</h2>
      <div class="cards">
        <div class="card"><div class="k">采暖季 月均用气</div><div class="v">{_m3(season['heating']['avg_usage_m3'])}</div>
          <div class="s">{season['heating']['count']} 个月 · 共 {_m3(season['heating']['total_usage_m3'])} m³</div></div>
        <div class="card"><div class="k">非采暖季 月均用气</div><div class="v">{_m3(season['off']['avg_usage_m3'])}</div>
          <div class="s">{season['off']['count']} 个月 · 共 {_m3(season['off']['total_usage_m3'])} m³</div></div>
      </div>"""

    yearly_rows = "".join(
        f"<tr><td>{r['year']}</td><td>{r['months']}</td><td>{_m3(r['total_usage_m3'])}</td>"
        f"<td>¥{_money(r['total_cost_yuan'])}</td><td>{_m3(r['avg_usage_m3'])}</td>"
        f"<td>¥{_money(r['avg_cost_yuan'])}</td></tr>"
        for r in yearly
    )

    monthly_rows = "".join(
        f"<tr><td>{b['yrMonth']}</td><td>{_m3(b['usage_m3'])}</td>"
        f"<td>¥{_money(b['cost_yuan'])}</td><td>{_money(b['avg_price'])}</td></tr>"
        for b in bills
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>港华燃气用气统计报告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 body{{font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;
   background:#f5f7fa;color:#1f2937;margin:0;padding:24px}}
 h1{{margin:0 0 4px}} .sub{{color:#6b7280;margin-bottom:20px}}
 h2{{margin-top:32px;border-left:4px solid #ef4444;padding-left:10px}}
 h3{{margin-top:20px;color:#374151}}
 .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}}
 .card{{background:#fff;border-radius:12px;padding:16px 18px;flex:1;min-width:180px;
   box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 .card .k{{color:#6b7280;font-size:13px}} .card .v{{font-size:26px;font-weight:700;color:#111827;margin:4px 0}}
 .card .s{{color:#9ca3af;font-size:12px}}
 table{{border-collapse:collapse;width:100%;background:#fff;margin-top:10px;border-radius:10px;overflow:hidden}}
 th,td{{padding:9px 12px;text-align:right;border-bottom:1px solid #eef0f3}}
 th:first-child,td:first-child{{text-align:left}}
 thead th{{background:#111827;color:#fff;font-weight:600}}
 tbody tr:nth-child(even){{background:#fafbfc}}
 .chart-box{{background:#fff;border-radius:12px;padding:16px;margin-top:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
</style></head><body>
<h1>港华燃气 · 用气统计分析报告</h1>
<div class="sub">生成时间 {datetime.now():%Y-%m-%d %H:%M} · 数据来源：港华 VCC 网上营业厅历史账单</div>
{note}

<h2>年度汇总</h2>
<table><thead><tr><th>年份</th><th>月数</th><th>总用气量 (m³)</th><th>总金额 (元)</th>
<th>月均用气 (m³)</th><th>月均金额 (元)</th></tr></thead>
<tbody>{yearly_rows}</tbody></table>

{yoy_html}

{season_html}

<h2>月度用量 / 金额趋势</h2>
<div class="chart-box"><canvas id="c1" height="90"></canvas></div>
<h2>实际单位气价趋势</h2>
<div class="chart-box"><canvas id="c2" height="80"></canvas></div>

<h2>月度明细</h2>
<table><thead><tr><th>账期</th><th>用气量 (m³)</th><th>金额 (元)</th><th>实际单价 (元/m³)</th></tr></thead>
<tbody>{monthly_rows}</tbody></table>

<script>
const labels={json.dumps(monthly_labels)};
new Chart(document.getElementById('c1'),{{type:'bar',data:{{
  labels,datasets:[
    {{type:'bar',label:'用气量 m³',data:{json.dumps(usage_series)},
      backgroundColor:'rgba(239,68,68,.7)',yAxisID:'y'}},
    {{type:'line',label:'金额 元',data:{json.dumps(cost_series)},
      borderColor:'#2563eb',backgroundColor:'#2563eb',yAxisID:'y1',tension:.3}}
  ]}},options:{{plugins:{{legend:{{position:'top'}}}},
  scales:{{y:{{position:'left',title:{{display:true,text:'m³'}}}},
    y1:{{position:'right',title:{{display:true,text:'元'}},grid:{{drawOnChartArea:false}}}}}}}}}});
new Chart(document.getElementById('c2'),{{type:'line',data:{{
  labels,datasets:[{{label:'实际单价 元/m³',data:{json.dumps(price_series)},
    borderColor:'#059669',backgroundColor:'rgba(5,150,105,.15)',fill:true,tension:.3}}]}},
  options:{{plugins:{{legend:{{position:'top'}}}}}}}});
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="港华燃气用气统计分析")
    ap.add_argument("--base-url", default="https://maanshan.towngasvcc.com")
    ap.add_argument("--token", default=None, help="access_token（--demo 时可选）")
    ap.add_argument("--refresh-token", default=None)
    ap.add_argument("--subs-code", default=None)
    ap.add_argument("--org-code", default=None)
    ap.add_argument("--out", default="./towngas_stats_out")
    ap.add_argument("--focus-month", type=int, default=2,
                    help="逐年对比聚焦的月份，默认 2 月")
    ap.add_argument("--demo", action="store_true",
                    help="使用内置示例账单生成演示报告（不联网、非真实数据）")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    note = ""
    if args.demo:
        print("[demo] 使用内置示例账单，生成演示报告（非真实数据）", file=sys.stderr)
        note = ('<div style="background:#fef3c7;color:#92400e;padding:10px 14px;'
                'border-radius:10px;margin:10px 0">⚠️ 演示报告：以下为内置示例数据，'
                '非真实账单。粘贴真实 access_token 运行（不加 --demo）即可得到真实统计。</div>')
        raw = build_sample_bills()
    else:
        if not args.token:
            print("缺少 --token（或加 --demo 生成示例报告）", file=sys.stderr)
            return 2
        client = TownGasStatsClient(args.base_url, args.token, args.refresh_token)
        try:
            # access_token 寿命仅约 15 分钟，先续期再取数
            client.ensure_token()

            if args.subs_code and args.org_code:
                subs_code, org_code = args.subs_code, args.org_code
            else:
                subs = client.get_bound_subs()
                if not subs:
                    print("未找到绑定户号，请手动传入 --subs-code / --org-code",
                          file=sys.stderr)
                    return 2
                s0 = subs[0]
                subs_code = s0.get("subsCode") or s0.get("subs_code")
                org_code = s0.get("orgCode") or s0.get("org_code")
                print(f"使用户号 subsCode={subs_code} orgCode={org_code}", file=sys.stderr)

            raw = client.get_bills(subs_code, org_code)
        except Exception as e:  # noqa: BLE001
            print(f"拉取失败：{e}", file=sys.stderr)
            return 1

    if not raw:
        print("该户号无历史账单数据。", file=sys.stderr)
        return 0

    bills = normalize_bills(raw)
    yearly = compute_yearly(bills)
    yoy = compute_yoy(yearly)
    month_compare = compute_month_compare(bills, args.focus_month)
    season = compute_season(bills)

    csv_path = os.path.join(args.out, "towngas_usage.csv")
    html_path = os.path.join(args.out, "towngas_report.html")
    write_csv(bills, csv_path)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(bills, yearly, yoy, month_compare, season, args.focus_month, note))

    # 控制台摘要
    print("\n===== 年度汇总 =====", file=sys.stderr)
    for r in yearly:
        print(f"  {r['year']}：{r['months']} 个月 · "
              f"用气 {r['total_usage_m3']} m³ · 金额 ¥{r['total_cost_yuan']} · "
              f"月均 {r['avg_usage_m3']} m³ / ¥{r['avg_cost_yuan']}", file=sys.stderr)
    if yoy:
        last = yoy[-1]
        print(f"\n===== {last['prev_year']}→{last['year']} 同比 =====", file=sys.stderr)
        print(f"  用量 {_pct(last['usage_yoy'])}，金额 {_pct(last['cost_yoy'])}", file=sys.stderr)
    print(f"\n输出：\n  CSV  : {csv_path}\n  HTML : {html_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
