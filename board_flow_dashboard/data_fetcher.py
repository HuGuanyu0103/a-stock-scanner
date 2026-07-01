#!/usr/bin/env python3
"""
A 股概念板块资金流向数据获取层

数据源：东方财富网-概念资金流排名 + 板块分时资金流向
底层依赖 akshare，可降级为模拟数据（非交易时段/网络不可达时）
"""

import json
import logging
import random
import time
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

TRADE_DATE = "2026-06-30"

SECTORS_WARM = [
    "光通信模块", "通信设备", "人形机器人", "数据中心", "玻璃基板",
    "商业航天", "AI芯片", "半导体", "军工", "消费电子",
    "低空经济", "可控核聚变", "光伏设备", "固态电池",
]
SECTORS_NEUTRAL = ["证券", "医药商业"]
SECTORS_COLD = [
    "白酒", "稀土永磁", "锂电池", "银行", "创新药",
    "黄金概念", "有色金属", "电网概念", "存储芯片",
]
ALL_SECTORS = SECTORS_WARM + SECTORS_NEUTRAL + SECTORS_COLD

COLOR_PALETTE = [
    "#FF4D4F", "#FF7A45", "#FAAD14", "#FADB14", "#A0D911",
    "#36CFC9", "#1890FF", "#597EF7", "#9254DE", "#F759AB",
    "#FF6347", "#FFB347", "#FFD700", "#ADFF2F", "#00CED1",
    "#4169E1", "#7B68EE", "#FF69B4", "#20B2AA", "#87CEEB",
    "#00BFFF", "#1E90FF", "#9370DB", "#FFA07A", "#98FB98",
]
SECTOR_COLORS = {s: COLOR_PALETTE[i] for i, s in enumerate(ALL_SECTORS)}


def _build_trade_minutes() -> list[str]:
    times = []
    h, m = 9, 31
    while h < 15 or (h == 15 and m == 0):
        times.append(f"{h:02d}:{m:02d}")
        m += 1
        if m == 60:
            h += 1
            m = 0
    times = [t for t in times if not ("11:31" <= t < "13:00")]
    return times


def _gen_simulated_cumulative(base_value: float, length: int,
                              noise_scale: float = 0.3,
                              drift: float = 0.02) -> list[float]:
    seq = []
    for i in range(length):
        progress = i / length
        target = base_value * progress
        noise = random.uniform(-noise_scale, noise_scale)
        val = target + noise + (random.uniform(-1, 1) * drift * base_value)
        seq.append(round(val, 2))
    if seq:
        seq[-1] = round(base_value, 2)
    return seq


def generate_mock_dashboard_data() -> dict:
    minutes = _build_trade_minutes()
    n_minutes = len(minutes)

    series_data = {}
    rank_data = []
    for sector in ALL_SECTORS:
        if sector in SECTORS_WARM:
            base = random.uniform(30, 130)
        elif sector in SECTORS_NEUTRAL:
            base = random.uniform(-10, 15)
        else:
            base = random.uniform(-80, -10)

        values = _gen_simulated_cumulative(base, n_minutes,
                                           noise_scale=abs(base) * 0.08,
                                           drift=0.03)
        series_data[sector] = {
            "name": sector,
            "color": SECTOR_COLORS[sector],
            "times": minutes,
            "values": values,
        }
        rank_data.append({
            "name": sector,
            "value": round(base, 2),
            "color": SECTOR_COLORS[sector],
        })

    rank_data.sort(key=lambda x: x["value"], reverse=True)

    return {
        "date": TRADE_DATE,
        "time_label": "15:00",
        "time_index": n_minutes - 1,
        "total_times": n_minutes,
        "minutes": minutes,
        "rank": rank_data,
        "series": {s["name"]: s for s in series_data.values()},
    }


def fetch_rank_akshare() -> Optional[pd.DataFrame]:
    import akshare as ak
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日",
                                            sector_type="概念资金流")
        if df is None or df.empty:
            return None
        cols = ["名称", "今日涨跌幅", "今日主力净流入-净额"]
        available = [c for c in cols if c in df.columns]
        result = df[available].copy()
        result.columns = ["name", "pct_chg", "net_main"]
        result["net_main"] = result["net_main"] / 1e8
        result["net_main"] = result["net_main"].round(2)
        return result
    except Exception as e:
        logger.warning("fetch_rank_akshare failed: %s", e)
        return None


def fetch_all_sectors_snapshot(timeout: float = 10.0) -> Optional[dict]:
    import requests as req
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "200", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "m:90+t:3",
            "fields": "f12,f14,f3,f62,f184,f66,f72,f78,f84",
            "_": str(int(time.time() * 1000)),
        }
        resp = req.get(url, params=params, timeout=timeout, headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://data.eastmoney.com/",
        })
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("diff", [])
        if not items:
            return None

        sectors = []
        for item in items:
            name = item.get("f14", "")
            if not name:
                continue
            sectors.append({
                "name": name,
                "code": item.get("f12", ""),
                "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
                "pct_chg": item.get("f3", 0),
            })
        if not sectors:
            return None
        return {
            "time": datetime.now().strftime("%H:%M"),
            "sectors": sectors,
        }
    except Exception as e:
        logger.warning("fetch_all_sectors_snapshot failed: %s", e)
        return None


def fetch_dashboard_data(use_real: bool = True) -> dict:
    if not use_real:
        return generate_mock_dashboard_data()

    rank_df = fetch_rank_akshare()
    snapshot = fetch_all_sectors_snapshot()

    if rank_df is None and snapshot is None:
        logger.info("真实数据不可用，降级为模拟数据")
        return generate_mock_dashboard_data()

    if snapshot is not None:
        sectors = snapshot["sectors"]
        top_sectors = sectors[:25]
    elif rank_df is not None:
        top_count = min(25, len(rank_df))
        top_sectors = [
            {"name": row["name"], "net_main": float(row["net_main"]),
             "pct_chg": float(row.get("pct_chg", 0))}
            for _, row in rank_df.head(top_count).iterrows()
        ]
    else:
        return generate_mock_dashboard_data()

    minutes = _build_trade_minutes()
    time_label = snapshot["time"] if snapshot else "15:00"

    rank_data = []
    series_data = {}
    for sec in top_sectors:
        name = sec["name"]
        value = sec["net_main"]
        color = SECTOR_COLORS.get(name, "#666666")
        rank_data.append({
            "name": name, "value": value, "color": color,
        })
        n = len(minutes)
        values = [None] * (n - 1) + [value] if n > 0 else [value]
        series_data[name] = {
            "name": name, "color": color,
            "times": minutes, "values": values,
        }

    rank_data.sort(key=lambda x: x["value"], reverse=True)

    return {
        "date": TRADE_DATE,
        "time_label": time_label,
        "time_index": len(minutes) - 1 if minutes else 0,
        "total_times": len(minutes),
        "minutes": minutes,
        "rank": rank_data,
        "series": series_data,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_dashboard_data(use_real=False)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:500])
    print(f"\n... 共 {len(data['rank'])} 个板块, {data['total_times']} 个时间点")

# ── 盘中选股模拟数据 ────────────────────────────────────────
_MOCK_STOCKS = [
    ("600519", "贵州茅台", 1680.0, 2.1, "白酒"),
    ("300750", "宁德时代", 198.5, 3.2, "固态电池"),
    ("601318", "中国平安", 52.3, 1.5, "保险"),
    ("000858", "五粮液", 142.0, 1.8, "白酒"),
    ("600036", "招商银行", 38.6, -0.5, "银行"),
    ("002371", "北方华创", 185.0, 4.2, "半导体"),
    ("688041", "海光信息", 78.5, 5.1, "AI芯片"),
    ("300308", "中际旭创", 126.8, 3.8, "光通信模块"),
    ("300502", "新易盛", 88.6, 4.5, "数据中心"),
    ("002475", "立讯精密", 35.8, 2.8, "消费电子"),
    ("000063", "中兴通讯", 32.5, 3.5, "通信设备"),
    ("600406", "国电南瑞", 25.6, -1.2, "电网概念"),
    ("600276", "恒瑞医药", 45.2, 0.8, "创新药"),
    ("601012", "隆基绿能", 22.3, -2.5, "光伏设备"),
    ("002460", "赣锋锂业", 38.5, -1.8, "锂电池"),
    ("300124", "汇川技术", 58.2, 2.2, "人形机器人"),
    ("600760", "中航沈飞", 42.6, 3.0, "军工"),
    ("002472", "双环传动", 18.5, 4.8, "人形机器人"),
    ("601899", "紫金矿业", 18.2, -0.3, "有色金属"),
    ("688012", "中微公司", 142.0, 2.6, "存储芯片"),
    ("002023", "海特高新", 18.2, 5.5, "低空经济"),
    ("600118", "中国卫星", 25.8, 3.2, "商业航天"),
    ("000777", "中核科技", 14.5, 1.5, "可控核聚变"),
    ("688300", "联瑞新材", 42.3, 2.0, "玻璃基板"),
    ("600489", "中金黄金", 15.8, -0.8, "黄金概念"),
    ("600030", "中信证券", 22.8, 1.2, "证券"),
    ("601607", "上海医药", 18.5, 0.5, "医药商业"),
    ("600010", "包钢股份", 3.6, -2.0, "稀土永磁"),
    ("601398", "工商银行", 5.8, -0.3, "银行"),
    ("600809", "山西汾酒", 186.0, 1.5, "白酒"),
    ("002594", "比亚迪", 268.0, 2.5, "固态电池"),
    ("688111", "金山办公", 285.0, 3.5, "信创"),
    ("002230", "科大讯飞", 48.5, 4.0, "AI芯片"),
    ("300033", "同花顺", 158.0, 2.8, "金融科技"),
    ("601688", "华泰证券", 18.2, 1.0, "证券"),
    ("002129", "中环股份", 32.5, -1.5, "光伏设备"),
    ("600031", "三一重工", 16.8, 1.2, "工程机械"),
    ("000001", "平安银行", 12.3, 0.8, "银行"),
    ("000725", "京东方A", 4.2, 1.0, "消费电子"),
    ("000100", "TCL科技", 4.5, 1.5, "消费电子"),
]

SIGNAL_POOL = [
    "放量突破", "MA5金叉MA10", "均线多头排列", "MACD金叉",
    "KDJ超卖金叉", "RSI上穿50", "连续3日放量", "涨停回踩10日线",
    "平台突破", "连板梯队", "炸板回封", "弱转强反包", "情绪周期",
]


def generate_mock_stock_picks() -> dict:
    """生成模拟的个股选股结果（匹配 run_daily.py 输出格式）"""
    picks = []
    for code, name, base_price, base_pct, sector in _MOCK_STOCKS:
        import random as rnd
        tech_score = rnd.randint(0, 8)
        sent_score = rnd.randint(0, 4)
        factor_score = round(rnd.uniform(0, 5), 1)
        combined = round(tech_score * 0.5 + sent_score * 0.2 + factor_score * 0.3, 1)
        n_signals = min(tech_score // 2 + sent_score // 2 + rnd.randint(0, 1), 5)
        signals = rnd.sample(SIGNAL_POOL, min(n_signals, len(SIGNAL_POOL)))
        price_jitter = rnd.uniform(-0.5, 0.5)
        pct_jitter = rnd.uniform(-0.3, 0.3)

        picks.append({
            "stock_code": code,
            "short_name": name,
            "price": round(base_price + price_jitter, 2),
            "change_pct": round(base_pct + pct_jitter, 1),
            "signal_score": tech_score,
            "sentiment_score": sent_score,
            "factor_score": factor_score,
            "combined_score": combined,
            "signal_count": len(signals),
            "signal_names": " | ".join(signals),
            "concepts": sector,
        })

    picks.sort(key=lambda x: x["combined_score"], reverse=True)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(picks),
        "stocks": picks,
    }
