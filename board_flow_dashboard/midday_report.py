#!/usr/bin/env python3
"""
午盘简报生成器 — 12:50 上午收盘后自动生成并推送微信

数据来源:
  - 沪深指数: push2 API
  - 板块资金流: push2 API (实时采集)
  - 候选池复盘: decision_store (早盘推荐记录)
  - 新闻快讯: 财联社 cls.cn
  - 市场情绪: push2 API + sentiment_collector

推送渠道: Server酱 (config.yaml)
"""

from __future__ import annotations

import json, logging, os, sys, time
from datetime import datetime, date
from pathlib import Path

import requests as req
import urllib3
urllib3.disable_warnings()

logger = logging.getLogger(__name__)
_http = req.Session()
_http.trust_env = False
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

try:
    from agent import get_agent, agent_brief_to_markdown
    from decision_store import get_decision_store
    _HAS_AI = True
except ImportError:
    _HAS_AI = False

# ═══════════════════════════════════════════════════════════════
# 数据采集
# ═══════════════════════════════════════════════════════════════

def _fetch_json(url, params, timeout=6):
    try:
        resp = _http.get(url, params=params, timeout=timeout, verify=False, headers=HEADERS)
        return resp.json()
    except Exception as e:
        logger.debug("API fail: %s", e)
        return {}

def fetch_indices() -> dict:
    """获取沪深主要指数上午表现。"""
    idx_map = {"1.000001": "上证指数", "0.399001": "深证成指", "0.399006": "创业板指"}
    result = {}
    for code, name in idx_map.items():
        data = _fetch_json("https://push2.eastmoney.com/api/qt/stock/get", {
            "secid": code, "fields": "f43,f44,f45,f46,f57,f58,f170",
            "_": str(int(time.time() * 1000)),
        })
        d = data.get("data", {}) or {}
        pct = d.get("f170")
        vol = d.get("f47") or d.get("f45")  # volume
        result[name] = {"pct_chg": round(float(pct)/100, 2) if pct else 0, "volume": vol}
    return result

def fetch_sector_flow(count=8) -> list:
    """获取上午概念板块资金流 Top N。"""
    data = _fetch_json("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": "1", "pz": str(count), "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f62",
        "fs": "m:90+t:3",
        "fields": "f12,f14,f3,f62,f184",
        "_": str(int(time.time() * 1000)),
    })
    items = data.get("data", {}).get("diff", [])
    return [{"name": i.get("f14",""), "pct_chg": i.get("f3",0),
             "net_main": round(float(i.get("f62",0))/1e8,2)} for i in items[:count]]

def fetch_market_breadth() -> dict:
    """上午涨跌家数。"""
    data = _fetch_json("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f3",
        "_": str(int(time.time() * 1000)),
    }, timeout=10)
    items = data.get("data", {}).get("diff", [])
    up = sum(1 for i in items if i.get("f3", 0) > 0)
    down = sum(1 for i in items if i.get("f3", 0) < 0)
    return {"up": up, "down": down, "total": len(items),
            "up_ratio": round(up/max(len(items),1),2)}

def fetch_news_headlines() -> list[dict]:
    """获取午间新闻快讯。"""
    try:
        url = "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=8.4.6"
        resp = _http.post(url, json={"type":"telegram","page":1,"rn":15}, timeout=8, verify=False, headers=HEADERS)
        items = resp.json().get("data", {}).get("roll_data", [])
        result = []
        for item in items[:10]:
            title = (item.get("title") or "").strip()
            if title and len(title) > 3:
                sentiment = _judge_sentiment(title)
                result.append({"title": title[:60], "sentiment": sentiment})
        return result
    except Exception:
        return []

def _judge_sentiment(title: str) -> str:
    """简单规则判断新闻情绪。"""
    pos = ["利好","增长","突破","签约","中标","补贴","扶持"]
    neg = ["下跌","亏损","减持","处罚","立案","退市","风险"]
    if any(k in title for k in pos): return "positive"
    if any(k in title for k in neg): return "negative"
    return "neutral"

def fetch_morning_picks() -> list:
    """获取早盘推荐记录（从 decision_store 获取今天的 shadow 记录）。"""
    try:
        ds = get_decision_store()
        today = date.today().strftime("%Y-%m-%d")
        # Get shadow records for today
        with ds._get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_code, stock_name, sector, signal, recommend_price "
                "FROM shadow_decisions WHERE entry_date=? ORDER BY agent_confidence DESC LIMIT 5",
                (today,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

# ═══════════════════════════════════════════════════════════════
# 报告生成
# ═══════════════════════════════════════════════════════════════

def generate_midday_report() -> str:
    """生成午盘简报。"""
    today_str = datetime.now().strftime("%m/%d")

    indices = fetch_indices()
    sectors = fetch_sector_flow(8)
    breadth = fetch_market_breadth()
    news = fetch_news_headlines()
    morning_picks = fetch_morning_picks()

    # 构建上下文
    idx_lines = []
    for name, info in indices.items():
        idx_lines.append(f"{name}{info['pct_chg']:+.2f}%")

    up_inflow = [s for s in sectors if s["net_main"] > 0][:4]
    down_sectors = [s for s in sectors if s["net_main"] < 0][:2]

    news_pos = [n for n in news if n["sentiment"] == "positive"]
    news_neg = [n for n in news if n["sentiment"] == "negative"]

    # 判断策略姿态
    up_ratio = breadth.get("up_ratio", 0.5)
    if up_ratio > 0.6:
        posture = "进攻"
        strategy = "上午赚钱效应良好，午后维持仓位，关注14:30尾盘"
    elif up_ratio > 0.4:
        posture = "均衡"
        strategy = "上午震荡整理，午后等待方向，不做大幅加减"
    else:
        posture = "防守"
        strategy = "上午偏弱，午后谨慎，如有反弹适当减仓"

    lines = [
        f"══ 午盘简报 {today_str} 12:50 ══",
        "",
        f"【上午复盘】{' '.join(idx_lines)} | 策略：{posture}，{strategy}",
        f"  • 涨跌比 {breadth['up']}:{breadth['down']}，上涨占比{int(up_ratio*100)}%",
        f"  • 成交量较昨日……（需实时数据对比）",
        "",
    ]

    # 板块轮动
    if up_inflow:
        inflow_names = "、".join(s["name"] for s in up_inflow[:3])
        lines.append(f"【板块轮动】{inflow_names}强者恒强")
        for s in up_inflow:
            lines.append(f"  • {s['name']}：主力净流入{s['net_main']:+.1f}亿")
    if down_sectors:
        outflow_names = "、".join(s["name"] for s in down_sectors[:2])
        lines.append(f"  退潮板块：{outflow_names}")
        for s in down_sectors:
            lines.append(f"  • {s['name']}：主力净流出{s['net_main']:+.1f}亿")
    lines.append("")

    # 早盘推荐复盘
    if morning_picks:
        lines.append("【早盘推荐复盘】")
        for mp in morning_picks[:3]:
            name = mp["stock_name"]
            code = mp["stock_code"]
            signal = mp.get("signal", "")
            price = mp.get("recommend_price", 0)
            lines.append(f"  • {name} {code} | {signal} | 建议价¥{price:.2f} → 午后跟踪")
        lines.append("")
    else:
        lines.append("【早盘推荐复盘】（今日暂无早盘推荐记录）\n")

    # 消息面
    if news:
        lines.append("【消息面·午间】财联社快讯")
        if news_pos:
            lines.append(f"  利好：{len(news_pos)}条 → {news_pos[0]['title'][:40] if news_pos else ''}")
        if news_neg:
            lines.append(f"  利空：{len(news_neg)}条 → {news_neg[0]['title'][:40] if news_neg else ''}")
        if not news_pos and not news_neg:
            lines.append("  上午无重大消息，市场按自身节奏运行")
        lines.append("")

    # 午后策略
    lines.append(f"【午后策略】{strategy}")

    return "\n".join(lines)

# ── Agent 增强（如果可用）───────────────────────────────────
def generate_with_agent() -> str:
    if not _HAS_AI:
        return generate_midday_report()
    return generate_midday_report()  # Agent enhancement deferred


