#!/usr/bin/env python3
"""
盘前简报生成器 — 9:25 竞价结束后自动生成并推送微信

用法:
  python3 pre_market_report.py           # 生成报告并推送
  python3 pre_market_report.py --dry-run  # 只生成不推送

数据来源:
  - 隔夜外盘: 东方财富全球指数 (push2)
  - 集合竞价: 全市场行情快照 (push2)
  - 板块热度: 概念板块排名 (push2)

推送渠道: Server酱 (config.yaml)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests as req
import urllib3

# Agent 集成
try:
    from agent import DecisionAgent, get_agent, agent_brief_to_markdown
    from key_stock_selector import select_key_stocks
    _HAS_AGENT = True
except ImportError:
    _HAS_AGENT = False
urllib3.disable_warnings()

logger = logging.getLogger(__name__)

_http = req.Session()
_http.trust_env = False
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

# ═══════════════════════════════════════════════════════════════
# 数据采集
# ═══════════════════════════════════════════════════════════════


def _fetch_json(url: str, params: dict, timeout: float = 6.0) -> dict:
    try:
        resp = _http.get(url, params=params, timeout=timeout,
                         verify=False, headers=HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("API 失败: %s", e)
        return {}


def fetch_overnight_indices() -> list[dict]:
    """获取隔夜外盘指数涨跌幅。"""
    symbols = {
        "100.NDX": "纳斯达克",
        "100.SPX": "标普500",
        "100.DJIA": "道指",
        "100.HSI": "恒生指数",
    }
    indices = []
    for secid, label in symbols.items():
        data = _fetch_json("https://push2.eastmoney.com/api/qt/stock/get", {
            "secid": secid,
            "fields": "f43,f57,f58,f170",
            "_": str(int(time.time() * 1000)),
        })
        d = data.get("data") or {}
        pct_raw = d.get("f170")   # f170 是涨跌幅×100，不用 f43(价格)兜底
        if pct_raw is not None:
            indices.append({
                "name": label,
                "pct_chg": round(float(pct_raw) / 100, 2),
            })
        else:
            indices.append({"name": label, "pct_chg": None})
    return indices


def fetch_auction_market_breadth() -> dict:
    """全市场竞价情绪统计。"""
    data = _fetch_json("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f3,f17,f18",
        "_": str(int(time.time() * 1000)),
    }, timeout=10.0)

    items = data.get("data", {}).get("diff", [])
    if not items:
        return {"total": 0, "up_ratio": 0.5, "median_pct": 0, "status": "no_data"}

    pcts = []
    up_count = 0
    for item in items:
        pct = item.get("f3")
        if pct is not None:
            p = float(pct)
            pcts.append(p)
            if p > 0:
                up_count += 1

    if not pcts:
        return {"total": len(items), "up_ratio": 0.5, "median_pct": 0, "status": "no_valid_pct"}

    pcts.sort()
    median = pcts[len(pcts) // 2]
    up_ratio = up_count / len(pcts) if pcts else 0.5

    status = "bullish" if median > 0.5 else ("bearish" if median < -0.5 else "neutral")
    return {
        "total": len(pcts),
        "up_count": up_count,
        "down_count": len(pcts) - up_count,
        "up_ratio": round(up_ratio, 2),
        "median_pct": round(median, 2),
        "status": status,
    }


def fetch_auction_anomalies(top_n: int = 10) -> list[dict]:
    """获取竞价异动股：高开 + 竞价放量。"""
    data = _fetch_json("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": "1", "pz": "200", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f8,f17,f18,f37,f47,f62,f184",
        "_": str(int(time.time() * 1000)),
    }, timeout=10.0)

    items = data.get("data", {}).get("diff", [])
    candidates = []
    for item in items:
        pct = item.get("f3")
        vol_ratio = item.get("f37")
        if pct is None:
            continue

        pct_val = float(pct)
        vol = float(vol_ratio) if vol_ratio else 0

        # 筛选：涨幅>2% 或 量比>3
        if pct_val > 2 or vol > 3:
            # 计算跳空幅度
            open_price = item.get("f17")
            preclose = item.get("f18")
            gap = 0
            if open_price and preclose:
                try:
                    gap = round((float(open_price) / float(preclose) - 1) * 100, 1)
                except (ValueError, ZeroDivisionError):
                    pass

            candidates.append({
                "code": item.get("f12", ""),
                "name": item.get("f14", ""),
                "pct_chg": round(pct_val, 1),
                "gap": gap,
                "volume_ratio": round(vol, 1),
                "net_main_ratio": item.get("f184"),
            })

    # 按涨幅 × 量比 综合排序
    candidates.sort(key=lambda x: abs(x["pct_chg"]) * x["volume_ratio"], reverse=True)
    return candidates[:top_n]


def fetch_auction_hot_sectors(top_n: int = 6) -> list[dict]:
    """获取竞价阶段资金流入最强的板块（今日重点关注）。"""
    data = _fetch_json("https://push2.eastmoney.com/api/qt/clist/get", {
        "pn": "1", "pz": "200", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f62",
        "fs": "m:90+t:3",       # 概念板块
        "fields": "f12,f14,f3,f62,f184",
        "_": str(int(time.time() * 1000)),
    }, timeout=8.0)

    items = data.get("data", {}).get("diff", [])
    sectors = []
    for item in items[:top_n]:
        name = item.get("f14", "")
        if name:
            sectors.append({
                "name": name,
                "pct_chg": item.get("f3", 0),
                "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
            })
    return sectors


# ═══════════════════════════════════════════════════════════════
# 报告生成
# ═══════════════════════════════════════════════════════════════

def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "--"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def _generate_template_report(key_stocks=None) -> str:
    """生成盘前简报 Markdown 文本。"""
    now = datetime.now()
    today_str = now.strftime("%m/%d")

    # ── 1. 外盘 ──────────────────────────────────────────────
    indices = fetch_overnight_indices()

    us_lines = []
    a50_str = "--"
    hsi_str = "--"
    for idx in indices:
        name = idx["name"]
        pct = idx["pct_chg"]
        if name in ("纳斯达克", "标普500", "道指"):
            us_lines.append(f"{name[:2]}指{_fmt_pct(pct)}")
        elif name == "恒生指数":
            hsi_str = _fmt_pct(pct)

    us_str = "  ".join(us_lines)
    ov_line = f"恒指 {hsi_str}"

    # 判断外盘情绪：用恒指作为主要 A 股情绪代理
    hsi_val = next((i["pct_chg"] for i in indices if i["name"] == "恒生指数"), 0)
    if hsi_val is None:
        hsi_val = 0
    if hsi_val > 0.3:
        ov_sentiment = "外盘偏暖，科技成长占优"
    elif hsi_val < -0.3:
        ov_sentiment = "外盘偏冷，开盘谨慎"
    else:
        ov_sentiment = "外盘中性"

    # ── 2. 竞价情绪 ──────────────────────────────────────────
    breadth = fetch_auction_market_breadth()
    if breadth["status"] == "no_data":
        auc_line = "竞价数据暂未就绪（可能非交易日）"
        auc_sentiment = ""
    else:
        up_pct = int(breadth["up_ratio"] * 100)
        auc_line = f"涨跌比 {breadth['up_count']}:{breadth['down_count']}  中位数 {_fmt_pct(breadth['median_pct'])}"

        if breadth["status"] == "bullish":
            auc_sentiment = "→ A 池可积极"
        elif breadth["status"] == "bearish":
            auc_sentiment = "→ 偏防御，B 池为主"
        else:
            auc_sentiment = "→ 中性，正常策略"

    # ── 3. 竞价异动 Top 5 ────────────────────────────────────
    anomalies = fetch_auction_anomalies(top_n=5)
    anomaly_lines = []
    for i, a in enumerate(anomalies):
        gap_str = f" 跳空{_fmt_pct(a['gap'])}" if a["gap"] != 0 else ""
        anomaly_lines.append(f"• {a['name']} {_fmt_pct(a['pct_chg'])}{gap_str} 量比{a['volume_ratio']}x")

    if not anomaly_lines:
        anomaly_lines = ["（竞价无明显异动个股）"]

    # ── 重点关注个股 ─────────────────────────────────────────
    if key_stocks:
        for ks in key_stocks:
            price = ks.get("price", 0) or 0
            price_s = "¥{:.2f}".format(price) if price else "--"
            pct = ks.get("pct_chg", 0) or 0
            pct_s = "{:+.1f}%".format(pct)
            anomaly_lines.append(
                "• {} {} | {} | {} {} | {}".format(
                    ks["name"], ks["code"], ks.get("source",""),
                    price_s, pct_s, ks.get("reason","")))

    # ── 4. 重点关注板块 ──────────────────────────────────────
    hot_sectors = fetch_auction_hot_sectors(top_n=6)
    sector_lines = []
    for s in hot_sectors:
        flow_str = f"{s['net_main']:+.1f}亿" if s["net_main"] else ""
        sector_lines.append(f"• {s['name']} {_fmt_pct(s['pct_chg'])} {flow_str}")

    if not sector_lines:
        sector_lines = ["（板块数据暂未就绪）"]

    # ── 5. 今日策略 ──────────────────────────────────────────
    if breadth["status"] == "bullish" and hsi_val > 0.2:
        strategy = "进攻  A池为主(65%)  方向：竞价强势板块"
    elif breadth["status"] == "bearish" and hsi_val < -0.2:
        strategy = "防守  B池防守(65%)  谨慎追涨"
    elif breadth["status"] == "bullish":
        strategy = "均衡  A/B 50:50  开盘后确认方向"
    elif breadth["status"] == "bearish":
        strategy = "偏防守  B池为主(60%)  关注低吸机会"
    else:
        strategy = "均衡  等待盘中方向确认"

    # ── 组装报告（新版式：判断+策略置顶 → 外盘 → 情绪 → 板块 → 个股）──
    lines = [
        f"══ 盘前简报 {today_str} 09:25 ══",
        "",
        f"【今日判断】{ov_sentiment} | {strategy}",
        "",
        f"【外盘】{ov_sentiment}",
        f"  • 美股：{us_str}",
        f"  • 港股：{ov_line}",
        "",
        f"【竞价情绪】{auc_sentiment or auc_line}",
        "",
    ]

    # ── 板块置前 ────────────────────────────────────────────
    lines.append("【今日重点关注板块】")
    if sector_lines:
        lines.append(f"主力资金聚焦{'、'.join(s['name'] for s in hot_sectors[:3]) if hot_sectors else '竞价活跃板块'}")
        lines.extend(sector_lines)
    else:
        lines.append("（板块数据暂未就绪）")
    lines.append("")

    # ── 重点个股（合并竞价异动 + 筛选结果）─────────────────
    lines.append("【今日重点关注个股】")
    if anomaly_lines:
        lines.append(f"竞价异动 + 技术面共振，共关注{len(anomaly_lines)}只")
        lines.extend(anomaly_lines)
    else:
        lines.append("（暂无符合条件的标的）")
    lines.append("")

    return "\n".join(lines)



# ═══════════════════════════════════════════════════════════════
# 报告生成（Agent 优先，模板兜底）
# ═══════════════════════════════════════════════════════════════

def generate_report() -> str:
    """生成盘前简报 Markdown 文本。

    v5.0: Agent 优先模式
    1. 先尝试 AI Agent 生成自然语言决策简报
    2. Agent 不可用时（无 API Key / 网络异常），自动降级到模板模式
    """
    # 收集所有数据（数据采集逻辑不变）
    indices = fetch_overnight_indices()
    breadth = fetch_auction_market_breadth()
    anomalies = fetch_auction_anomalies(top_n=5)
    hot_sectors = fetch_auction_hot_sectors(top_n=6)
    key_stocks = select_key_stocks(anomalies, hot_sectors)

    # 尝试 AI Agent 生成
    if _HAS_AGENT:
        try:
            agent = get_agent()
            ks_ctx = ""
            if key_stocks:
                ks_lines = ["=== 今日重点关注个股 ==="]
                for ks in key_stocks:
                    ks_lines.append(
                        "{} {} | {} | ¥{:.2f} | {:+.1f}% | {}".format(
                            ks["name"], ks["code"], ks.get("source",""),
                            ks.get("price",0) or 0, ks.get("pct_chg",0) or 0,
                            ks.get("reason","")))
                ks_ctx = "\n".join(ks_lines)
            brief = agent.generate_pre_market_brief(
                indices, breadth, anomalies, hot_sectors,
                key_stocks_context=ks_ctx
            )
            if brief:
                logger.info("Agent 简报生成成功")
                return agent_brief_to_markdown(brief)
        except Exception as e:
            logger.warning("Agent 简报生成失败，降级到模板模式: %s", e)

    # Fallback: 模板模式
    logger.info("使用模板模式生成简报")
    return _generate_template_report(key_stocks)


# ═══════════════════════════════════════════════════════════════
# 推送
# ═══════════════════════════════════════════════════════════════

def send_report(report: str) -> bool:
    """通过 Server酱 推送到微信。"""
    try:
        parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent not in sys.path:
            sys.path.insert(0, parent)
        from push import send

        today = datetime.now().strftime("%m/%d")
        result = send(f"📊 盘前简报 {today}", report)
        if result.get("code") == 0:
            logger.info("盘前简报推送成功")
            return True
        else:
            logger.error("盘前简报推送失败: %s", result.get("message", "未知"))
            return False
    except Exception as e:
        logger.error("推送异常: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════
# 交易日判断
# ═══════════════════════════════════════════════════════════════

def _is_trading_day() -> bool:
    try:
        from data_fetcher import is_trading_day
        return is_trading_day(date.today())
    except ImportError:
        wd = date.today().weekday()
        return wd < 5


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    # 非交易日跳过
    if not _is_trading_day():
        logger.info("今日非交易日，跳过盘前简报")
        return

    # 检查时间 — 必须在 9:25 之后
    now = datetime.now()
    minute_of_day = now.hour * 60 + now.minute
    if minute_of_day < 9 * 60 + 25:
        logger.info("尚未到 9:25，跳过（当前 %s）", now.strftime("%H:%M"))
        return

    dry_run = "--dry-run" in sys.argv

    print("=" * 50)
    print("  盘前简报生成器")
    print(f"  时间: {now.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)
    print()

    report = generate_report()
    print(report)
    print()

    if dry_run:
        print("[DRY RUN] 未推送，仅预览")
        return

    success = send_report(report)
    if success:
        print("✅ 已推送到微信")
    else:
        print("❌ 推送失败，请检查 Server酱 配置")
        sys.exit(1)


if __name__ == "__main__":
    main()
