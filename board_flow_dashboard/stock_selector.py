#!/usr/bin/env python3
"""
盘中选股引擎

从热板块下钻到个股，结合资金流和量价因子评分，输出候选池。
适用周期：超短线（2-4 天持股）

数据流程：
  热板块排名 -> 板块成分股实时行情+资金流 -> 多因子归一化评分 -> 候选池
"""

import json
import logging
import random
import time
from datetime import datetime
from daily_scorer import load_daily_scores

logger = logging.getLogger(__name__)

# -- 策略参数 --
HOT_SECTOR_COUNT = 5
STOCKS_PER_SECTOR = 10
CANDIDATE_POOL_SIZE = 25

WEIGHTS = {
    "net_main_ratio": 0.35,
    "volume_ratio": 0.20,
    "price_deviation": 0.25,
    "turnover_rate": 0.10,
    "amp_ratio": 0.10,
}

_MOCK_STOCKS_BY_SECTOR = {
    "\u5149\u901a\u4fe1\u6a21\u5757": [
        ("300308", "\u4e2d\u9645\u65ed\u521b"), ("000063", "\u4e2d\u5174\u901a\u8baf"),
        ("688313", "\u4ed5\u4f73\u5149\u5b50"), ("300502", "\u65b0\u6613\u76db"), ("300394", "\u5929\u592b\u901a\u4fe1"),
    ],
    "\u4eba\u5f62\u673a\u5668\u4eba": [
        ("688160", "\u6b65\u79d1\u80a1\u4efd"), ("002472", "\u53cc\u73af\u4f20\u52a8"), ("300124", "\u6c47\u5ddd\u6280\u672f"),
        ("688017", "\u7eff\u7684\u8c10\u6ce2"), ("002747", "\u57c3\u65af\u987f"),
    ],
    "AI\u82af\u7247": [
        ("688041", "\u6d77\u5149\u4fe1\u606f"), ("603986", "\u5146\u6613\u521b\u65b0"), ("002049", "\u7d2b\u5149\u56fd\u5fae"),
        ("300782", "\u5353\u80dc\u5fae"), ("688256", "\u5bd2\u6b66\u7eaa"),
    ],
    "\u6570\u636e\u4e2d\u5fc3": [
        ("000977", "\u6d6a\u6f6e\u4fe1\u606f"), ("603019", "\u4e2d\u79d1\u66e6\u5149"),
        ("002415", "\u6d77\u5eb7\u5a01\u89c6"), ("000938", "\u7d2b\u5149\u80a1\u4efd"), ("688111", "\u91d1\u5c71\u529e\u516c"),
    ],
    "\u534a\u5bfc\u4f53": [
        ("688981", "\u4e2d\u82af\u56fd\u9645"), ("600703", "\u4e09\u5b89\u5149\u7535"),
        ("688012", "\u4e2d\u5fae\u516c\u53f8"), ("300223", "\u5317\u4eac\u541b\u6b63"), ("688072", "\u62d3\u8346\u79d1\u6280"),
    ],
    "\u4f4e\u7a7a\u7ecf\u6d4e": [
        ("002085", "\u4e07\u4e30\u5965\u5a01"), ("300719", "\u5b89\u8fbe\u7ef4\u5c14"), ("688070", "\u7eb5\u6a2a\u80a1\u4efd"),
        ("002023", "\u6d77\u7279\u9ad8\u65b0"), ("600879", "\u822a\u5929\u7535\u5b50"),
    ],
    "\u56fa\u6001\u7535\u6c60": [
        ("002074", "\u56fd\u8f69\u9ad8\u79d1"), ("300750", "\u5b81\u5fb7\u65f6\u4ee3"), ("002709", "\u5929\u8d50\u6750\u6599"),
        ("300014", "\u4ebf\u97cb\u94c1\u80fd"), ("300568", "\u661f\u6e90\u6750\u8d28"),
    ],
    "\u901a\u4fe1\u8bbe\u5907": [
        ("600745", "\u95fb\u6cf0\u79d1\u6280"), ("002281", "\u5149\u8baf\u79d1\u6280"),
        ("603160", "\u6c47\u9876\u79d1\u6280"), ("002396", "\u661f\u7f51\u9510\u6377"), ("300628", "\u4ebf\u8054\u7f51\u7edc"),
    ],
    "\u519b\u5de5": [
        ("600760", "\u4e2d\u822a\u6c88\u98de"), ("002179", "\u4e2d\u822a\u5149\u7535"), ("600893", "\u822a\u53d1\u52a8\u529b"),
        ("600862", "\u4e2d\u822a\u9ad8\u79d1"), ("000768", "\u4e2d\u822a\u897f\u98de"),
    ],
}


def _minmax_normalize(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return {v: 0.5 for v in values}
    return {v: (v - lo) / (hi - lo) for v in values}


def _infer_signal(s):
    net_ratio = s.get("net_main_ratio") or 0
    vol_ratio = s.get("volume_ratio") or 0
    pct = s.get("pct_chg") or 0
    dev = s.get("price_deviation") or 0
    if net_ratio > 15 and vol_ratio > 1.5 and pct > 3:
        return "\u653e\u91cf\u4e0a\u653b"
    if net_ratio > 10 and dev < -1:
        return "\u8865\u6da8\u6f5c\u529b"
    if net_ratio > 8:
        return "\u8d44\u91d1\u9a71\u52a8"
    if vol_ratio > 2 and pct > 2:
        return "\u653e\u91cf\u7a81\u7834"
    if vol_ratio > 1.5 and net_ratio > 5:
        return "\u91cf\u4ef7\u9f50\u5347"
    if dev < -2:
        return "\u6ede\u6da8\u5173\u6ce8"
    if net_ratio > 3:
        return "\u6e29\u548c\u5438\u7b79"
    return "\u76d8\u4e2d\u89c2\u5bdf"


def _score_and_rank(stocks):
    if not stocks:
        return stocks

    factor_keys = list(WEIGHTS.keys())
    norms = {}
    for key in factor_keys:
        raw = [s.get(key) or 0 for s in stocks]
        norms[key] = _minmax_normalize(raw)

    lo_pd = min(s.get("price_deviation") or 0 for s in stocks)
    hi_pd = max(s.get("price_deviation") or 0 for s in stocks)

    for s in stocks:
        for key in factor_keys:
            raw_v = s.get(key) or 0
            s["norm_" + key] = norms[key][raw_v]

        raw_dev = s.get("price_deviation") or 0
        if hi_pd != lo_pd:
            s["norm_price_deviation"] = 1 - (raw_dev - lo_pd) / (hi_pd - lo_pd)
        else:
            s["norm_price_deviation"] = 0.5

        s["score"] = round(
            sum(s["norm_" + k] * w for k, w in WEIGHTS.items()), 4
        )
        s["signal"] = _infer_signal(s)

    stocks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return stocks


# -- 实时数据采集 --

# -- 日评分合并 --

def _merge_daily_scores(stocks, scores_map):
    """将日级别评分（run_daily.py 结果）合并到候选股中。"""
    if not scores_map:
        for s in stocks:
            s["daily_combined_score"] = None
            s["daily_signal_count"] = 0
            s["daily_signal_names"] = ""
        return stocks
    for s in stocks:
        key = s.get("code", "")
        d = scores_map.get(key)
        if d:
            s["daily_combined_score"] = d["combined_score"]
            s["daily_signal_count"] = d["signal_count"]
            s["daily_signal_names"] = d.get("signal_names", "")
        else:
            s["daily_combined_score"] = None
            s["daily_signal_count"] = 0
            s["daily_signal_names"] = ""
    return stocks



def _fetch_hot_sectors(top_n=5):
    import requests as req
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": "200", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "m:90+t:3", "fields": "f12,f14,f3,f62,f184",
            "_": str(int(time.time() * 1000))}
        resp = req.get(url, params=params, timeout=8, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
            "Referer": "https://data.eastmoney.com/"})
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("diff", [])
        sectors = []
        for item in items[:top_n]:
            code, name = item.get("f12", ""), item.get("f14", "")
            if code and name:
                sectors.append({"code": code, "name": name,
                    "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
                    "pct_chg": item.get("f3", 0)})
        return sectors
    except Exception as e:
        logger.warning("\u83b7\u53d6\u70ed\u677f\u5757\u5931\u8d25: %s", e)
        return []


def _fetch_sector_stocks(board_code, top_n=10):
    import requests as req
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": str(top_n), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "b:" + board_code + "+f:!50",
            "fields": "f12,f14,f2,f3,f8,f37,f62,f66,f184",
            "_": str(int(time.time() * 1000))}
        resp = req.get(url, params=params, timeout=8, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
            "Referer": "https://data.eastmoney.com/"})
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("diff", [])
        stocks = []
        for item in items:
            stock = {"code": item.get("f12", ""), "name": item.get("f14", ""),
                "price": item.get("f2"), "pct_chg": item.get("f3"),
                "turnover_rate": item.get("f8"), "volume_ratio": item.get("f37"),
                "net_main_inflow": (round(float(item.get("f62", 0)) / 1e8, 2)
                                     if item.get("f62") else 0),
                "amp_ratio": item.get("f66"), "net_main_ratio": item.get("f184")}
            if stock["code"] and stock["name"]:
                stocks.append(stock)
        return stocks
    except Exception as e:
        logger.warning("\u83b7\u53d6\u677f\u5757\u6210\u5206\u80a1(%s)\u5931\u8d25: %s", board_code, e)
        return []


def _select_stocks_real():
    hot_sectors = _fetch_hot_sectors()
    if not hot_sectors:
        raise RuntimeError("\u70ed\u677f\u5757\u6570\u636e\u4e3a\u7a7a")

    all_stocks = []
    for sector in hot_sectors:
        stocks = _fetch_sector_stocks(sector["code"])
        for s in stocks:
            s["sector"] = sector["name"]
            s["sector_pct"] = sector["pct_chg"]
            s["price_deviation"] = round(
                (s.get("pct_chg") or 0) - (sector["pct_chg"] or 0), 2)
        all_stocks.extend(stocks)

    if not all_stocks:
        raise RuntimeError("\u65e0\u6210\u5206\u80a1\u6570\u636e")

    seen = set()
    unique = []
    for s in all_stocks:
        if s["code"] not in seen:
            seen.add(s["code"])
            unique.append(s)

    ranked = _score_and_rank(unique)
    ranked = _merge_daily_scores(ranked, _DAILY_SCORES)
    return {"time": datetime.now().strftime("%H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(ranked), "candidates": ranked[:CANDIDATE_POOL_SIZE],
        "hot_sectors": [s["name"] for s in hot_sectors], "mode": "live"}


def _select_stocks_mock():
    sectors = list(_MOCK_STOCKS_BY_SECTOR.keys())[:HOT_SECTOR_COUNT]
    result = []
    for sector in sectors:
        stocks_list = _MOCK_STOCKS_BY_SECTOR[sector]
        sector_pct = round(random.uniform(0.5, 4.0), 2)
        for code, name in stocks_list:
            pct_chg = round(random.uniform(-2, 6), 2)
            result.append({"code": code, "name": name, "sector": sector,
                "price": round(random.uniform(10, 200), 2),
                "pct_chg": pct_chg,
                "net_main_inflow": round(random.uniform(-3, 12), 2),
                "net_main_ratio": round(random.uniform(-5, 20), 2),
                "volume_ratio": round(random.uniform(0.3, 3.0), 2),
                "turnover_rate": round(random.uniform(0.5, 10.0), 1),
                "amp_ratio": round(random.uniform(1, 7), 1),
                "sector_pct": sector_pct,
                "price_deviation": round(pct_chg - sector_pct, 2)})

    ranked = _score_and_rank(result)
    ranked = _merge_daily_scores(ranked, _DAILY_SCORES)

    # 如果在 mock 模式下日评分为空，生成模拟日评分
    if not _DAILY_SCORES:
        signal_bank = ["放量突破", "MA5金叉MA10", "均线多头排列", "MACD金叉",
                       "KDJ超卖金叉", "RSI上穿50", "连续3日放量", "涨停回踩10日线",
                       "平台突破", "连板梯队", "情绪周期"]
        for c in ranked:
            n_sig = random.randint(1, 5)
            c["daily_combined_score"] = round(random.uniform(2, 12), 1)
            c["daily_signal_count"] = n_sig
            c["daily_signal_names"] = " | ".join(random.sample(signal_bank, min(n_sig, len(signal_bank))))

    return {"time": datetime.now().strftime("%H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(ranked), "candidates": ranked[:CANDIDATE_POOL_SIZE],
        "hot_sectors": sectors, "mode": "mock"}


_DAILY_SCORES = {}
_CACHE = {}
_CACHE_TTL = 0.0
_CACHE_LIFETIME = 60.0


def select_stocks(use_mock=False):
    global _CACHE, _CACHE_TTL, _DAILY_SCORES

    # 首次调用时加载日评分缓存
    if not _DAILY_SCORES:
        _DAILY_SCORES.update(load_daily_scores())
        if _DAILY_SCORES:
            logger.info("加载日评分数据: %d 只股票", len(_DAILY_SCORES))

    now = time.time()
    if not use_mock and _CACHE and (now - _CACHE_TTL) < _CACHE_LIFETIME:
        return _CACHE

    if use_mock:
        result = _select_stocks_mock()
    else:
        try:
            result = _select_stocks_real()
        except Exception as e:
            logger.warning("\u5b9e\u65f6\u9009\u80a1\u5931\u8d25\uff0c\u964d\u7ea7\u4e3a\u6a21\u62df: %s", e)
            result = _select_stocks_mock()

    if not use_mock:
        _CACHE = result
        _CACHE_TTL = now
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = select_stocks(use_mock=True)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
    print(f"\n\u5171 {data['total']} \u53ea\u5019\u9009 \u00b7 \u6a21\u5f0f={data['mode']}")
    for c in data['candidates'][:10]:
        print(f"  {c['name']:6s} {c['sector']:6s} "
              f"\u6da8{c['pct_chg']:>+5.1f}% "
              f"\u4e3b\u529b{c['net_main_inflow']:>5.1f}\u4ebf "
              f"\u51c0\u5360\u6bd4{c['net_main_ratio']:>5.1f}% "
              f"\u91cf\u6bd4{c['volume_ratio']:>4.1f} "
              f"\u8bc4\u5206{c['score']:.3f} {c['signal']}")
