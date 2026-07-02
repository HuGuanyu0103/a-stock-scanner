#!/usr/bin/env python3
"""
盘前预扫描模块 v4.0

填补 9:15-9:30 的信号空白，在开盘前提供：
  1. 隔夜外盘映射 — 美股/港股/富时期货涨跌 → A 股板块情绪倾向
  2. 集合竞价分析 — 9:15-9:25 竞价量价异常检测

供 app.py 在开盘前调用，结果写入 SignalStore 或直接返回前端。

用法:
  from pre_market import PreMarketScanner
  scanner = PreMarketScanner()
  signal = scanner.scan()  # 返回盘前信号摘要
"""

import json
import logging
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
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

# ── 外盘 → A 股板块映射 ─────────────────────────────────────

OVERSEAS_SECTOR_MAP = {
    # 美股指数 → 关联 A 股板块
    "nasdaq": {
        "positive": ["AI芯片", "半导体", "消费电子", "光通信模块", "数据中心",
                      "人形机器人", "存储芯片", "通信设备", "低空经济"],
        "negative": ["银行", "白酒", "医药商业"],
    },
    "sp500": {
        "positive": ["证券", "银行", "新能源车", "消费电子"],
        "negative": [],
    },
    "dow": {
        "positive": ["银行", "证券", "白酒", "有色金属"],
        "negative": [],
    },
    # 港股映射
    "hsi": {
        "positive": ["证券", "银行", "白酒", "新能源车", "创新药"],
        "negative": [],
    },
    "hscei": {  # 恒生中国企业指数
        "positive": ["银行", "证券", "通信设备"],
        "negative": [],
    },
    # 富时 A50 期货
    "a50": {
        "positive": ["证券", "银行", "白酒", "新能源车", "有色金属", "创新药"],
        "negative": [],
    },
}

# ── 外盘数据获取 ────────────────────────────────────────────

def _fetch_us_index(symbol: str) -> Optional[dict]:
    """获取美股指数昨收涨跌幅（用东方财富全球指数接口）。"""
    try:
        code_map = {
            "nasdaq": "100.NDX",    # 纳斯达克 100
            "sp500": "100.SPX",     # 标普 500
            "dow": "100.DJIA",      # 道琼斯
        }
        code = code_map.get(symbol, "")
        if not code:
            return None

        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": code,
            "fields": "f43,f44,f45,f46,f57,f58,f60,f169,f170",
            "_": str(int(time.time() * 1000)),
        }
        resp = _http.get(url, params=params, timeout=6, verify=False, headers=HEADERS)
        data = resp.json().get("data", {})
        if not data:
            return None

        pct = data.get("f170") or data.get("f43")  # f170=涨跌幅, f43=最新价
        name = data.get("f58", symbol)
        return {"name": name, "pct_chg": float(pct) if pct else 0}
    except Exception as e:
        logger.debug("获取美股 %s 失败: %s", symbol, e)
        return None


def _fetch_hk_index() -> Optional[dict]:
    """获取恒生指数昨收涨跌幅。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": "100.HSI",
            "fields": "f43,f57,f58,f170",
            "_": str(int(time.time() * 1000)),
        }
        resp = _http.get(url, params=params, timeout=6, verify=False, headers=HEADERS)
        data = resp.json().get("data", {})
        if not data:
            return None
        pct = data.get("f170") or data.get("f43")
        return {"name": "恒生指数", "pct_chg": float(pct) if pct else 0}
    except Exception as e:
        logger.debug("获取恒生指数失败: %s", e)
        return None


def _fetch_a50_futures() -> Optional[dict]:
    """获取富时 A50 期货最新涨跌幅（通过东方财富全球期货接口）。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": "100.CN02",   # 富时 A50
            "fields": "f43,f57,f58,f170",
            "_": str(int(time.time() * 1000)),
        }
        resp = _http.get(url, params=params, timeout=6, verify=False, headers=HEADERS)
        data = resp.json().get("data", {})
        if not data:
            return None
        pct = data.get("f170") or data.get("f43")
        return {"name": "富时A50期货", "pct_chg": float(pct) if pct else 0}
    except Exception as e:
        logger.debug("获取 A50 期货失败: %s", e)
        return None


# ── 盘前信号计算 ────────────────────────────────────────────

def compute_overnight_sentiment() -> dict:
    """计算隔夜外盘映射的 A 股板块情绪。

    Returns:
        {
            "indices": {name: pct_chg, ...},
            "positive_sectors": [...],   # 看多板块
            "negative_sectors": [...],   # 看空板块
            "overall_sentiment": float,  # -1.0 ~ +1.0
            "summary": str,              # 一句话描述
        }
    """
    indices = {}
    sector_scores: dict[str, float] = {}  # sector → accumulated score

    # 美股三大指数
    for sym in ("nasdaq", "sp500", "dow"):
        d = _fetch_us_index(sym)
        if d:
            indices[sym] = d["pct_chg"]
            mapping = OVERSEAS_SECTOR_MAP.get(sym, {})
            pct = d["pct_chg"]
            if pct > 0.3:
                for sec in mapping.get("positive", []):
                    sector_scores[sec] = sector_scores.get(sec, 0) + min(pct / 2, 1.5)
                for sec in mapping.get("negative", []):
                    sector_scores[sec] = sector_scores.get(sec, 0) + max(pct / 2, -1.5)
            elif pct < -0.3:
                for sec in mapping.get("positive", []):
                    sector_scores[sec] = sector_scores.get(sec, 0) + max(pct / 2, -1.5)
                for sec in mapping.get("negative", []):
                    sector_scores[sec] = sector_scores.get(sec, 0) + min(-pct / 2, 1.5)

    # 港股
    hk = _fetch_hk_index()
    if hk:
        indices["hsi"] = hk["pct_chg"]
        pct = hk["pct_chg"]
        mapping = OVERSEAS_SECTOR_MAP.get("hsi", {})
        if pct > 0.3:
            for sec in mapping.get("positive", []):
                sector_scores[sec] = sector_scores.get(sec, 0) + min(pct / 2, 1.5)
        elif pct < -0.3:
            for sec in mapping.get("negative", []):
                sector_scores[sec] = sector_scores.get(sec, 0) + max(pct / 2, -1.5)

    # A50 期货（最直接）
    a50 = _fetch_a50_futures()
    if a50:
        indices["a50"] = a50["pct_chg"]
        pct = a50["pct_chg"]
        mapping = OVERSEAS_SECTOR_MAP.get("a50", {})
        if abs(pct) > 0.2:
            for sec in mapping.get("positive", []):
                sector_scores[sec] = sector_scores.get(sec, 0) + pct * 0.8

    # 聚合
    pos_sectors = sorted(
        [(k, v) for k, v in sector_scores.items() if v > 0],
        key=lambda x: -x[1]
    )[:10]
    neg_sectors = sorted(
        [(k, v) for k, v in sector_scores.items() if v < 0],
        key=lambda x: x[1]
    )[:5]

    if sector_scores:
        overall = sum(sector_scores.values()) / max(len(sector_scores), 1)
        overall = max(-3.0, min(3.0, overall)) / 3.0  # 归一化到 [-1, 1]
    else:
        overall = 0

    if overall > 0.3:
        summary = f"隔夜外盘偏暖，A50{'涨' if a50 and a50['pct_chg'] > 0 else '平'}，关注科技成长"
    elif overall < -0.3:
        summary = f"隔夜外盘偏冷，A50{'跌' if a50 and a50['pct_chg'] < 0 else '平'}，谨慎追涨"
    else:
        summary = "隔夜外盘中性，等待开盘确认方向"

    logger.info("盘前外盘映射: overall=%.2f %s", overall, summary)

    return {
        "indices": indices,
        "positive_sectors": [s[0] for s in pos_sectors],
        "negative_sectors": [s[0] for s in neg_sectors],
        "overall_sentiment": round(overall, 2),
        "summary": summary,
        "time": datetime.now().strftime("%H:%M"),
    }


# ── 集合竞价分析（简化版）───────────────────────────────────

def _fetch_auction_data(count: int = 30) -> list:
    """获取集合竞价阶段量价异动个股（9:15-9:25）。

    使用东方财富行情接口，筛选竞价量比异常的标的。
    """
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(count), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f3",     # 按涨跌幅排序
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f8,f17,f37,f62,f184",
            "_": str(int(time.time() * 1000)),
        }
        resp = _http.get(url, params=params, timeout=8, verify=False, headers=HEADERS)
        items = resp.json().get("data", {}).get("diff", [])
        result = []
        for item in items:
            pct = item.get("f3", 0)
            vol_ratio = item.get("f37", 0)
            # 竞价异动：涨幅 > 2% 或 量比 > 3
            if abs(float(pct) if pct else 0) > 2 or (float(vol_ratio) if vol_ratio else 0) > 3:
                result.append({
                    "code": item.get("f12", ""),
                    "name": item.get("f14", ""),
                    "price": item.get("f2"),
                    "pct_chg": pct,
                    "open_price": item.get("f17"),  # 今开（竞价阶段为暂定开盘价）
                    "volume_ratio": vol_ratio,
                    "net_main_ratio": item.get("f184"),
                })
        return result
    except Exception as e:
        logger.debug("获取竞价数据失败: %s", e)
        return []


def _time_in_auction() -> bool:
    """判断是否在集合竞价时段（9:15-9:25）。"""
    now = datetime.now()
    return now.hour == 9 and 15 <= now.minute <= 25


# ── 盘前扫描器主类 ──────────────────────────────────────────

class PreMarketScanner:
    """盘前预扫描器。

    使用方式:
        scanner = PreMarketScanner()
        signal = scanner.scan()
        # signal 包含 overnight + auction 两部分
    """

    def __init__(self):
        self._last_scan: dict = {}
        self._last_scan_time: float = 0
        self._cache_ttl: float = 120  # 2 分钟缓存

    def scan(self) -> dict:
        """执行完整盘前扫描。

        Returns:
            {
                "overnight": {...},     # 隔夜外盘映射
                "auction": [...],       # 集合竞价异动
                "in_auction": bool,     # 是否竞价时段
                "pre_market_open": bool, # 是否盘前（9:30前）
                "summary": str,         # 综合盘前判断
            }
        """
        now = time.time()
        if self._last_scan and (now - self._last_scan_time) < self._cache_ttl:
            return self._last_scan

        is_pre_market = datetime.now().hour < 9 or \
            (datetime.now().hour == 9 and datetime.now().minute < 30)
        in_auction = _time_in_auction()

        overnight = compute_overnight_sentiment()

        auction = []
        if in_auction:
            auction = _fetch_auction_data(count=30)
            if auction:
                logger.info("集合竞价异动: %d 只", len(auction))

        # 综合盘前判断
        overall = overnight.get("overall_sentiment", 0)
        if overall > 0.3 and in_auction and auction:
            summary = "外盘偏暖 + 竞价有异动 → 开盘偏乐观，关注竞价放量方向"
        elif overall < -0.3:
            summary = "外盘偏冷 → 开盘谨慎，等待盘中确认"
        elif in_auction and len(auction) > 10:
            summary = "竞价活跃度较高 → 关注竞价最强方向"
        elif is_pre_market:
            summary = "距开盘还有一段时间，等待竞价阶段"
        else:
            summary = "已过盘前时段"

        result = {
            "overnight": overnight,
            "auction": auction[:15],     # 只保留 Top 15 异动
            "in_auction": in_auction,
            "pre_market_open": is_pre_market,
            "summary": summary,
            "scan_time": datetime.now().strftime("%H:%M:%S"),
        }

        self._last_scan = result
        self._last_scan_time = now
        return result

    def apply_to_candidates(self, candidates: list, pool_type: str = "A") -> list:
        """将盘前情绪施加到候选池。

        正向板块加分，反向板块减分。仅调整 score，不改变入选状态。

        Args:
            candidates: 候选股票列表
            pool_type: "A" 或 "B"（B 池不受外盘情绪影响）

        Returns:
            调整后的候选列表
        """
        if pool_type == "B":
            return candidates  # B 池不受外盘情绪影响

        signal = self.scan()
        overnight = signal.get("overnight", {})
        pos_sectors = set(overnight.get("positive_sectors", []))
        neg_sectors = set(overnight.get("negative_sectors", []))
        overall = overnight.get("overall_sentiment", 0)

        if abs(overall) < 0.2:
            return candidates  # 外盘中性，不改动

        for c in candidates:
            sector = c.get("sector", "")
            if sector in pos_sectors:
                c["score"] = round(c["score"] * (1 + abs(overall) * 0.05), 4)
                c["overnight_boost"] = True
            elif sector in neg_sectors and overall < 0:
                c["score"] = round(c["score"] * (1 + overall * 0.05), 4)
                c["overnight_penalty"] = True

        logger.info("盘前情绪施加: A池 %d 只, overall=%.2f", len(candidates), overall)
        return candidates


# ── CLI 测试 ────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    scanner = PreMarketScanner()
    result = scanner.scan()
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
