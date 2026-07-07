#!/usr/bin/env python3
"""
盘前重点关注个股筛选器

三个池子独立筛选，去重合并输出 Top 5：
  池1「竞价异动」：跳空高开 + 量比放大（竞价强度）
  池2「热板块龙头」：热板块成分股中主力资金最强的标的
  池3「技术突破」：日评分缓存中形态最好的标的

筛选硬规则：
  - 价格 3-30 元
  - 排除 688（科创板）、300/301（创业板）
  - 排除 ST/*ST
  - 小盘股（流通市值 < 50亿）需满足额外条件才能入选

用法:
  from key_stock_selector import select_key_stocks
  stocks = select_key_stocks(anomalies, hot_sectors)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import requests as req
import urllib3
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

DATA_DIR = Path(__file__).parent / "data"

# ── 筛选参数 ─────────────────────────────────────────────────

MIN_PRICE = 3.0
MAX_PRICE = 30.0
MIN_MARKET_CAP = 50         # 流通市值（亿），低于此值需破格条件
MIN_MARKET_CAP_EXCEPTION = 30  # 绝对下限
MAX_KEY_STOCKS = 5
EXCLUDE_PREFIXES = ("688", "300", "301", "8", "9")

# 破格条件：小盘股必须同时满足
EXCEPTION_VOLUME_RATIO = 5.0     # 量比 > 5
EXCEPTION_NET_MAIN_RATIO = 10.0  # 主力净占比 > 10%
EXCEPTION_DAILY_SCORE = 8.0      # 日评分 > 8（池3专用）


# ── 池1：竞价异动精选 ───────────────────────────────────────

def _pool_auction(anomalies: list[dict]) -> list[dict]:
    """从竞价异动中筛选。"""
    candidates = []
    for a in anomalies:
        code = a.get("code", "")
        name = a.get("name", "")
        if not code or not name:
            continue
        # 硬过滤
        if code.startswith(EXCLUDE_PREFIXES):
            continue
        if "ST" in name or "退" in name:
            continue
        # 价格过滤（竞价异动数据无价格字段，后续补）
        # 竞价强度评分
        gap = a.get("gap", 0) or 0
        vol = a.get("volume_ratio", 0) or 0
        pct = a.get("pct_chg", 0) or 0
        intensity = abs(gap) * vol * (1 + abs(pct) * 0.1)
        candidates.append({
            "code": code,
            "name": name,
            "pct_chg": pct,
            "gap": gap,
            "volume_ratio": vol,
            "net_main_ratio": a.get("net_main_ratio"),
            "score": round(intensity, 2),
            "source": "竞价异动",
            "reason": "",
        })

    candidates.sort(key=lambda x: -x["score"])
    return candidates[:3]


# ── 池2：热板块龙头 ─────────────────────────────────────────

def _fetch_sector_stocks(sector_code: str, top_n: int = 30) -> list[dict]:
    """获取板块成分股（主力净流入排序）。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(top_n), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": f"b:{sector_code}",
            "fields": "f12,f14,f2,f3,f8,f15,f16,f17,f20,f21,f37,f62,f184",
            "_": str(int(time.time() * 1000)),
        }
        resp = _http.get(url, params=params, timeout=6, verify=False, headers=HEADERS)
        items = resp.json().get("data", {}).get("diff", [])
        result = []
        for item in items:
            price = item.get("f2")
            market_cap = item.get("f21")  # f21 = 流通市值（元）
            cap_yi = round(float(market_cap) / 1e8, 1) if market_cap else 0
            result.append({
                "code": item.get("f12", ""),
                "name": item.get("f14", ""),
                "price": float(price) if price else 0,
                "pct_chg": item.get("f3", 0),
                "volume_ratio": item.get("f37", 0),
                "market_cap": cap_yi,
                "turnover_rate": item.get("f8", 0),
                "net_main_ratio": item.get("f184", 0),
            })
        return result
    except Exception as e:
        logger.debug("板块成分股获取失败 %s: %s", sector_code, e)
        return []


def _pool_sector_leaders(hot_sectors: list[dict]) -> list[dict]:
    """从热板块中筛选龙头股。"""
    # 取主力资金最强的 3 个板块
    top_sectors = sorted(hot_sectors, key=lambda x: -(x.get("net_main", 0)))[:3]

    # 获取每个板块的成分股
    sector_stocks_map: dict[str, list] = {}
    for s in top_sectors:
        sname = s.get("name", "")
        # 先获取板块代码
        scode = _get_sector_code(sname)
        if not scode:
            continue
        stocks = _fetch_sector_stocks(scode, top_n=30)
        if stocks:
            sector_stocks_map[sname] = stocks
            time.sleep(0.3)  # API 间隔

    # 评分 + 过滤
    candidates = []
    for sec_name, stocks in sector_stocks_map.items():
        for s in stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            price = s.get("price", 0)
            cap = s.get("market_cap", 0)

            # 硬过滤
            if code.startswith(EXCLUDE_PREFIXES):
                continue
            if "ST" in (name or "") or "退" in (name or ""):
                continue
            if price < MIN_PRICE or price > MAX_PRICE:
                continue

            # 市值检查
            vol = float(s.get("volume_ratio", 0) or 0)
            nr = float(s.get("net_main_ratio", 0) or 0)
            if cap < MIN_MARKET_CAP_EXCEPTION:
                continue  # 太小，直接排除
            if cap < MIN_MARKET_CAP:
                # 小盘股：需满足破格条件
                if not (vol > EXCEPTION_VOLUME_RATIO and nr > EXCEPTION_NET_MAIN_RATIO):
                    continue  # 不合格的小盘股

            # 评分
            score = nr * 0.6 + vol * 0.4
            candidates.append({
                "code": code,
                "name": name,
                "price": price,
                "pct_chg": s.get("pct_chg", 0),
                "volume_ratio": vol,
                "net_main_ratio": nr,
                "market_cap": cap,
                "sector": sec_name,
                "score": round(score, 2),
                "source": f"热板块·{sec_name}",
                "reason": "",
            })

    candidates.sort(key=lambda x: -x["score"])
    # 同一板块最多取 2 只
    seen_sectors: dict[str, int] = {}
    result = []
    for c in candidates:
        sec = c.get("sector", "")
        if seen_sectors.get(sec, 0) >= 2:
            continue
        seen_sectors[sec] = seen_sectors.get(sec, 0) + 1
        result.append(c)
    return result[:3]


def _get_sector_code(name: str) -> Optional[str]:
    """查找板块代码。先查本地缓存，再动态查。"""
    cache_file = DATA_DIR / "sector_codes.json"
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except Exception:
            pass
    if name in cache:
        return cache[name]

    # 动态查询
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "200", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "m:90+t:3",
            "fields": "f12,f14",
            "_": str(int(time.time() * 1000)),
        }
        resp = _http.get(url, params=params, timeout=6, verify=False, headers=HEADERS)
        items = resp.json().get("data", {}).get("diff", [])
        for item in items:
            n = item.get("f14", "")
            c = item.get("f12", "")
            if n and c:
                cache[n] = c
        cache_file.write_text(json.dumps(cache, ensure_ascii=False))
        return cache.get(name)
    except Exception:
        return None


# ── 池3：技术形态突破（日评分缓存）────────────────────────

def _pool_daily_breakout() -> list[dict]:
    """从日评分缓存中筛选技术面最优的标的。"""
    # 尝试加载最新的日评分缓存
    today = date.today().strftime("%Y%m%d")
    cache_path = DATA_DIR / f"daily_scores_{today}.json"
    if not cache_path.exists():
        # 尝试昨天的
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        cache_path = DATA_DIR / f"daily_scores_{yesterday}.json"
    if not cache_path.exists():
        return []

    try:
        scores = json.loads(cache_path.read_text())
    except Exception:
        return []

    candidates = []
    for code, info in scores.items():
        name = info.get("short_name", "")
        price = info.get("price", 0)
        cs = info.get("combined_score", 0)
        sig_count = info.get("signal_count", 0)
        sig_names = info.get("signal_names", "")

        # 硬过滤
        if code.startswith(EXCLUDE_PREFIXES):
            continue
        if "ST" in name or "退" in name:
            continue
        if price < MIN_PRICE or price > MAX_PRICE:
            continue

        # 日评分过滤
        if cs < 5:
            continue

        # 市值检查
        cap = info.get("market_cap", 0) or 0
        if cap < MIN_MARKET_CAP_EXCEPTION:
            continue
        if cap < MIN_MARKET_CAP:
            if cs < EXCEPTION_DAILY_SCORE:
                continue

        candidates.append({
            "code": code,
            "name": name,
            "price": price,
            "pct_chg": info.get("change_pct", 0),
            "daily_score": cs,
            "signal_count": sig_count,
            "signal_names": sig_names,
            "market_cap": cap,
            "score": cs,
            "source": "技术突破",
            "reason": sig_names,
            "volume_ratio": 0,
            "net_main_ratio": 0,
        })

    candidates.sort(key=lambda x: -x["score"])
    return candidates[:3]


# ── 合并 + 去重 + 生成理由 ──────────────────────────────────

def select_key_stocks(
    anomalies: list[dict],
    hot_sectors: list[dict],
) -> list[dict]:
    """三池合并去重，输出 Top 5 重点关注个股。

    Args:
        anomalies: fetch_auction_anomalies() 的返回
        hot_sectors: fetch_auction_hot_sectors() 的返回

    Returns:
        [{"code": str, "name": str, "price": float, "pct_chg": float,
          "volume_ratio": float, "source": str, "reason": str, ...}, ...]
    """
    pool1 = _pool_auction(anomalies)
    pool2 = _pool_sector_leaders(hot_sectors)
    pool3 = _pool_daily_breakout()

    # 去重（池1 > 池2 > 池3 优先级）
    seen = set()
    merged = []

    for p in pool1:
        if p["code"] not in seen:
            seen.add(p["code"])
            p["reason"] = _build_reason(p)
            merged.append(p)

    for p in pool2:
        if p["code"] not in seen:
            seen.add(p["code"])
            p["reason"] = _build_reason(p)
            merged.append(p)

    # 池3补充：如果 merged 不足 3 只，放宽到 5
    if len(merged) < 3:
        for p in pool3:
            if p["code"] not in seen:
                seen.add(p["code"])
                p["reason"] = _build_reason(p)
                merged.append(p)

    return merged[:MAX_KEY_STOCKS]


def _build_reason(s: dict) -> str:
    """为股票生成人类可读的入选理由。"""
    parts = []
    src = s.get("source", "")
    gap = s.get("gap", 0) or 0
    vol = s.get("volume_ratio", 0) or 0
    nr = s.get("net_main_ratio", 0) or 0
    pct = s.get("pct_chg", 0) or 0

    if "竞价" in src:
        parts.append(f"竞价跳空{gap:+.1f}%")
    if vol > 3:
        parts.append(f"量比{vol:.1f}倍")
    if nr > 5:
        parts.append(f"主力净占比{nr:+.1f}%")
    if "技术" in src:
        sig = s.get("signal_names", "") or s.get("reason", "")
        if sig:
            parts.append(sig[:30])

    base = " | ".join(parts) if parts else src
    return base


# ── CLI 测试 ────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    # 模拟数据
    mock_anomalies = [
        {"code":"000063","name":"中兴通讯","pct_chg":5.2,"gap":3.2,"volume_ratio":8.5,"net_main_ratio":12.3},
        {"code":"600519","name":"贵州茅台","pct_chg":1.5,"gap":0.8,"volume_ratio":4.2,"net_main_ratio":5.1},
        {"code":"002475","name":"立讯精密","pct_chg":3.8,"gap":2.1,"volume_ratio":6.0,"net_main_ratio":9.5},
        {"code":"300750","name":"宁德时代","pct_chg":2.1,"gap":1.0,"volume_ratio":3.5,"net_main_ratio":6.2},
    ]
    mock_sectors = [
        {"name":"光通信模块","net_main":8.5},
        {"name":"AI芯片","net_main":6.2},
        {"name":"新能源车","net_main":-1.2},
    ]

    result = select_key_stocks(mock_anomalies, mock_sectors)
    print(json.dumps(result, ensure_ascii=False, indent=2))
