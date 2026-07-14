#!/usr/bin/env python3
"""
A 股板块资金流向数据获取层

数据源：东方财富网 push2 API — 概念板块、行业板块、北向资金、个股资金流
底层依赖 akshare，可降级为模拟数据（非交易时段/网络不可达时）
"""

import json
import logging
import random
import subprocess
import time
from datetime import datetime, date
from typing import Optional

import pandas as pd
import requests as req
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 共享 Session，绕过 macOS 系统代理（东方财富 API 需直连）
_http = req.Session()
_http.trust_env = False

logger = logging.getLogger(__name__)

# ── 退避重试 + 熔断器 ──────────────────────────────────────────

_CIRCUIT_BREAKER: dict[str, list[float]] = {}  # source_name -> [fail_timestamps]
_CIRCUIT_COOLDOWN = 60  # 连续失败后冷却 60 秒
_CIRCUIT_THRESHOLD = 3   # 连续失败 3 次触发熔断


def _retry_with_backoff(fn, name: str = "api", max_retries: int = 3, base_delay: float = 1.0):
    """带指数退避的重试 + 熔断器。

    如果同一 name 连续失败 CIRCUIT_THRESHOLD 次，触发熔断，
    在 CIRCUIT_COOLDOWN 秒内直接跳过，不再尝试。
    """
    # 熔断检查
    failures = _CIRCUIT_BREAKER.get(name, [])
    now = time.time()
    recent_fails = [t for t in failures if now - t < _CIRCUIT_COOLDOWN]
    if len(recent_fails) >= _CIRCUIT_THRESHOLD:
        logger.warning("熔断器触发 [%s]: %d次失败/%d秒内，跳过重试", name, len(recent_fails), _CIRCUIT_COOLDOWN)
        raise ConnectionError(f"Circuit breaker open for {name}")

    last_exc = None
    for attempt in range(max_retries):
        try:
            result = fn()
            # 成功 → 清除该源的失败记录
            _CIRCUIT_BREAKER.pop(name, None)
            if attempt > 0:
                logger.info("重试成功 [%s] 第%d次", name, attempt + 1)
            return result
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.debug("重试 [%s] 第%d/%d次失败，%.1fs后重试: %s", name, attempt + 1, max_retries, delay, e)
                time.sleep(delay)

    # 全部失败 → 记录失败时间戳
    _CIRCUIT_BREAKER.setdefault(name, []).append(now)
    logger.warning("重试耗尽 [%s]: %d次全部失败", name, max_retries)
    raise last_exc


def reset_circuit_breakers(name: str = None):
    """重置熔断器（新交易日开始时调用）"""
    if name:
        _CIRCUIT_BREAKER.pop(name, None)
    else:
        _CIRCUIT_BREAKER.clear()
        logger.info("所有熔断器已重置")


def get_circuit_status() -> dict:
    """查询各数据源熔断状态"""
    now = time.time()
    status = {}
    for src, timestamps in _CIRCUIT_BREAKER.items():
        recent = [t for t in timestamps if now - t < _CIRCUIT_COOLDOWN]
        status[src] = {"failures": len(recent), "circuit_open": len(recent) >= _CIRCUIT_THRESHOLD}
    return status


# ── 腾讯行情 API fallback ─────────────────────────────────────

def _fetch_tencent_market(codes: list[str]) -> pd.DataFrame:
    """通过腾讯 qt.gtimg.cn API 获取实时行情（push2 的可靠降级）。

    腾讯 API 格式: http://qt.gtimg.cn/q=sh600519,sz000001
    返回字段(~分隔): 0=未知, 1=名称, 2=代码, 3=现价, 4=昨收, 5=今开,
                    6=成交量(手), 7=外盘, 8=内盘, 9=买一, 31=涨跌, 32=涨跌幅,
                    37=换手率, 45=市盈率, ...
    """
    if not codes:
        return pd.DataFrame()
    # 构建腾讯格式的股票代码
    tencent_codes = []
    for c in codes:
        c = str(c).zfill(6)
        prefix = "sh" if c.startswith("6") else "sz"
        tencent_codes.append(f"{prefix}{c}")
    url = f"http://qt.gtimg.cn/q={','.join(tencent_codes)}"
    try:
        resp = req.get(url, timeout=5)
        resp.encoding = 'gbk'
        rows = []
        for line in resp.text.strip().split(';\n'):
            if not line.strip() or '=' not in line:
                continue
            # 解析 var hq_str_xxx="..." 格式
            _, value = line.split('=', 1)
            value = value.strip().strip('"').strip("'")
            fields = value.split('~')
            if len(fields) < 33:
                continue
            raw_code = fields[2]
            rows.append({
                "stock_code": raw_code,
                "short_name": fields[1],
                "price": float(fields[3]) if fields[3] else 0,
                "change_pct": float(fields[32]) if fields[32] else 0,
                "change": float(fields[31]) if fields[31] else 0,
                "volume": int(fields[6]) if fields[6] else 0,  # 手
                "amount": int(fields[6]) * float(fields[3]) if fields[6] and fields[3] else 0,
                "turnover_rate": float(fields[38]) if len(fields) > 38 and fields[38] else 0,
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        logger.debug("腾讯行情 API 失败: %s", e)
        return pd.DataFrame()


def _fetch_tencent_kline(code: str, days: int = 120) -> pd.DataFrame:
    """通过腾讯 API 获取日K线（已有 _qq_kline，这里是统一接口）"""
    return pd.DataFrame()

TRADE_DATE = datetime.now().strftime("%Y-%m-%d")


def _now_time() -> str:
    """当前时间，收盘后（>15:00）封顶至 15:00，避免数据时间显示为收盘后。"""
    now = datetime.now()
    if now.hour > 15 or (now.hour == 15 and now.minute > 0):
        return "15:00"
    return now.strftime("%H:%M")


# ── 交易日历缓存 ────────────────────────────────────────────
_TRADING_DAYS: set[str] = set()
_TRADING_CALENDAR_LOADED = False


def load_trading_calendar(year: Optional[int] = None) -> set[str]:
    """加载 A 股交易日历，失败时降级为周一至周五判断。"""
    global _TRADING_DAYS, _TRADING_CALENDAR_LOADED
    if _TRADING_CALENDAR_LOADED and _TRADING_DAYS:
        return _TRADING_DAYS

    if year is None:
        year = datetime.now().year

    try:
        import akshare as ak
        # 拉取当年 + 明年的交易日
        for y in (year, year + 1):
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                col = df.columns[0]
                dates = df[col].astype(str).tolist()
                _TRADING_DAYS.update(d for d in dates if d >= f"{year}-01-01")
        _TRADING_CALENDAR_LOADED = True
        logger.info("交易日历加载完成: %d 个交易日", len(_TRADING_DAYS))
    except Exception as e:
        logger.warning("交易日历加载失败: %s，降级为周一至周五判断", e)
        _TRADING_CALENDAR_LOADED = True  # 标记已尝试，避免反复重试

    return _TRADING_DAYS


def is_trading_day(d: Optional[date] = None) -> bool:
    """判断是否为 A 股交易日。"""
    if d is None:
        d = date.today()

    # 周末一定不是交易日
    if d.weekday() >= 5:
        return False

    # 如果有交易日历缓存，精确判断
    if _TRADING_DAYS:
        return d.isoformat() in _TRADING_DAYS

    # 降级：周一至周五
    return True

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

EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}


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
        ratio = random.uniform(-15, 25)  # 模拟主力净占比
        ratio_values = _gen_simulated_cumulative(ratio, n_minutes,
                                                  noise_scale=abs(ratio) * 0.1,
                                                  drift=0.02)
        series_data[sector] = {
            "name": sector,
            "color": SECTOR_COLORS[sector],
            "times": minutes,
            "values": values,
            "ratio_values": ratio_values,
        }
        rank_data.append({
            "name": sector,
            "value": round(base, 2),
            "color": SECTOR_COLORS[sector],
            "net_main_ratio": round(ratio, 2),
        })

    rank_data.sort(key=lambda x: x["value"], reverse=True)

    return {
        "date": TRADE_DATE,
        "is_trading": _is_trading_time(),
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


def _fetch_all_via_curl_parallel(fs: str, timeout: float = 15.0) -> Optional[list[dict]]:
    """并行拉取 5 页，合并为同一时刻的全量快照，消除分页时间错位。"""
    import os as _os, urllib.parse as _up
    fields = "f12,f14,f3,f62,f184,f66,f72,f78,f84"
    base_params = {
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f62",
        "pz": "5000", "po": "1", "np": "1",
        "fs": fs, "fields": fields,
    }
    config_lines = []
    for pn in range(1, 6):
        params = {**base_params, "pn": str(pn)}
        url = "https://push2.eastmoney.com/api/qt/clist/get?" + _up.urlencode(params)
        config_lines.append(f'url = "{url}"')
        config_lines.append(f'output = "/tmp/curl_p{pn}.json"')
        config_lines.append('user-agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"')
        config_lines.append('referer = "https://data.eastmoney.com/"')
        config_lines.append('')
    config_path = "/tmp/curl_parallel.conf"
    with open(config_path, "w") as f:
        f.write("\n".join(config_lines))
    try:
        subprocess.run(
            ["curl", "-x", "http://127.0.0.1:7897", "--parallel",
             "--parallel-max", "5", "--max-time", str(int(timeout)),
             "-s", "-K", config_path],
            capture_output=True, timeout=timeout + 10,
        )
    except Exception:
        pass
    # 收集结果
    seen: dict[str, dict] = {}
    for pn in range(1, 6):
        path = f"/tmp/curl_p{pn}.json"
        if _os.path.exists(path):
            try:
                with open(path) as f:
                    body = json.load(f).get("data", {})
                for item in body.get("diff", []):
                    name = item.get("f14", "")
                    if not name:
                        continue
                    net_main = round(float(item.get("f62", 0)) / 1e8, 2)
                    entry = {
                        "name": name, "code": item.get("f12", ""),
                        "net_main": net_main,
                        "net_main_ratio": item.get("f184") or 0,
                        "pct_chg": item.get("f3", 0),
                    }
                    if name in seen:
                        if abs(net_main) > abs(seen[name]["net_main"]):
                            seen[name] = entry
                    else:
                        seen[name] = entry
            except Exception:
                pass
            try:
                _os.remove(path)
            except Exception:
                pass
    try:
        _os.remove(config_path)
    except Exception:
        pass
    if not seen:
        return None
    sectors = list(seen.values())
    sectors.sort(key=lambda x: x["net_main"], reverse=True)
    return sectors


def _fetch_via_curl(fs: str, timeout: float = 10.0, pn: int = 1, po: int = 1) -> Optional[list[dict]]:
    """curl 兜底：Python requests 无法走 Clash 代理时，用 curl 子进程取数据。"""
    import urllib.parse
    fields = "f12,f14,f3,f62,f184,f66,f72,f78,f84"
    params = {
        "pn": str(pn), "pz": "5000", "po": str(po), "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f62",
        "fs": fs, "fields": fields,
    }
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    proxy = "http://127.0.0.1:7897"
    cmd = ["curl", "-s", "--max-time", str(int(timeout)),
           "-x", proxy,
           "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
           "-H", "Referer: https://data.eastmoney.com/",
           url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout + 2)
        if result.returncode != 0 or not result.stdout:
            return None
        data = json.loads(result.stdout).get("data", {})
        items = data.get("diff", [])
        if not items:
            return None
        sectors = []
        for item in items:
            sectors.append({
                "name": item.get("f14", ""),
                "code": item.get("f12", ""),
                "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
                "net_main_ratio": item.get("f184") or 0,
                "pct_chg": item.get("f3", 0),
            })
        return sectors
    except Exception:
        return None


def _fetch_sectors_full(fs: str, timeout: float = 10.0) -> Optional[dict]:
    """拉取板块全量快照。

    API 的 pz 参数偶尔被忽略，回调默认 100。先尝试 pz=5000，
    若返回不足预期则逐页补齐并升序兜底，确保板块全覆盖。
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    seen: dict[str, dict] = {}
    min_expected = 80 if ":2" in fs else 400

    def _add_items(items):
        for item in items:
            name = item.get("f14", "")
            if not name:
                continue
            net_main = round(float(item.get("f62", 0)) / 1e8, 2)
            entry = {
                "name": name,
                "code": item.get("f12", ""),
                "net_main": net_main,
                "net_main_ratio": item.get("f184") or 0,
                "pct_chg": item.get("f3", 0),
            }
            if name in seen:
                if abs(net_main) > abs(seen[name]["net_main"]):
                    seen[name] = entry
            else:
                seen[name] = entry

    base_params = {
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f62",
        "fs": fs,
        "fields": "f12,f14,f3,f62,f184,f66,f72,f78,f84",
    }

    def _fetch_page(pn: int, po: int = 1) -> bool:
        """拉一页，Python requests 优先，带重试；失败则 curl 兜底。"""
        # Python requests 重试 3 次
        for attempt in range(3):
            try:
                params = {**base_params, "pn": str(pn), "pz": "5000", "po": str(po), "np": "1",
                          "_": str(int(time.time() * 1000))}
                resp = _http.get(url, params=params, timeout=timeout,
                               verify=False, headers=EASTMONEY_HEADERS)
                resp.raise_for_status()
                body = resp.json().get("data", {})
                _add_items(body.get("diff", []))
                nonlocal total_expected
                if total_expected == 0:
                    total_expected = body.get("total", 0)
                return True
            except Exception:
                if attempt < 2:
                    time.sleep(5)  # 重试前等 5 秒
        # curl 兜底
        curl_items = _fetch_via_curl(fs, timeout, pn, po)
        if curl_items:
            logger.info("curl 兜底成功 pn=%d po=%d, items=%d", pn, po, len(curl_items))
            _add_items(curl_items)
            if total_expected == 0:
                total_expected = len(curl_items)
            return True
        logger.info("curl 兜底失败 pn=%d po=%d", pn, po)
        return False

    # 并行拉取全量（无时间错位）
    parallel_sectors = _fetch_all_via_curl_parallel(fs, timeout)
    if parallel_sectors and len(parallel_sectors) >= min_expected * 0.6:
        _add_items(parallel_sectors)
        sectors = list(seen.values())
        sectors.sort(key=lambda x: x["net_main"], reverse=True)
        return {"time": _now_time(), "sectors": sectors}

    # 并行失败 → 降级单页
    total_expected = 0
    if not _fetch_page(1, 1):
        logger.warning("板块 API 全部方式失败: %s", fs)
        return None

    # 单页不够 → 顺序翻页补齐
    if total_expected > 0 and len(seen) < total_expected:
        logger.info("翻页补齐: %d/%d", len(seen), total_expected)
        for pn in range(2, 10):
            if len(seen) >= total_expected:
                break
            time.sleep(30)
            if not _fetch_page(pn, 1):
                logger.info("第%d页失败，15s后重试...", pn)
                time.sleep(15)
                if not _fetch_page(pn, 1):
                    logger.info("第%d页停止", pn)
                    break
            logger.info("第%d页: 累计 %d/%d", pn, len(seen), total_expected)

    sectors = list(seen.values())
    if not sectors:
        return None
    sectors.sort(key=lambda x: x["net_main"], reverse=True)
    return {"time": _now_time(), "sectors": sectors}


def fetch_all_sectors_snapshot(timeout: float = 10.0) -> Optional[dict]:
    """获取概念板块全量快照（降序拉取 + 升序兜底 + 退避重试 + 熔断）。"""
    try:
        return _retry_with_backoff(
            lambda: _fetch_sectors_full(fs="m:90+t:3", timeout=timeout),
            name="push2_concept", max_retries=4, base_delay=1.5,
        )
    except Exception as e:
        logger.warning("fetch_all_sectors_snapshot failed: %s", e)
        return None


def fetch_industry_sectors_snapshot(timeout: float = 10.0) -> Optional[dict]:
    """获取行业板块资金流向快照（降序拉取 + 升序兜底 + 退避重试 + 熔断）。"""
    try:
        return _retry_with_backoff(
            lambda: _fetch_sectors_full(fs="m:90+t:2", timeout=timeout),
            name="push2_industry", max_retries=4, base_delay=1.5,
        )
    except Exception as e:
        logger.warning("fetch_industry_sectors_snapshot failed: %s", e)
        return None


def fetch_northbound_flow(timeout: float = 8.0) -> Optional[dict]:
    """获取沪深港通（北向资金）实时流向。

    返回字段：
      - net_inflow: 北向净流入（亿元）= 沪股通 + 深股通
      - hk2sh:      沪股通净流入（亿元）
      - hk2sz:      深股通净流入（亿元）
      - time:        更新时间
    """
    try:
        return _retry_with_backoff(lambda: _fetch_northbound_raw(timeout),
                                   name="push2_northbound", max_retries=3, base_delay=1.0)
    except Exception as e:
        logger.warning("fetch_northbound_flow failed: %s", e)
        return None


def _fetch_northbound_raw(timeout: float = 8.0) -> Optional[dict]:
    url = "https://push2.eastmoney.com/api/qt/kamt/get"
    params = {"_": str(int(time.time() * 1000))}
    resp = _http.get(url, params=params, timeout=timeout,
                   verify=False, headers=EASTMONEY_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rc") != 0:
        return None
    d = data.get("data", {})
    hk2sh = d.get("hk2sh", {})
    hk2sz = d.get("hk2sz", {})
    net_sh = float(hk2sh.get("dayNetAmtIn", 0)) / 1e8
    net_sz = float(hk2sz.get("dayNetAmtIn", 0)) / 1e8
    total_net = net_sh + net_sz
    return {
        "time": _now_time(),
        "net_inflow": round(total_net, 2),
        "hk2sh": round(net_sh, 2),
        "hk2sz": round(net_sz, 2),
    }


def fetch_stock_fund_flow_rank(sort_by: str = "net_main",
                               count: int = 50,
                               timeout: float = 10.0) -> Optional[list]:
    """获取全 A 股主力资金净流入排名。

    Args:
        sort_by: 排序字段 — "net_main"(主力净额) 或 "net_ratio"(主力净占比)
        count: 返回数量
    """
    try:
        return _retry_with_backoff(
            lambda: _fetch_stock_flow_raw(sort_by, count, timeout),
            name="push2_stock_flow", max_retries=3, base_delay=1.0,
        )
    except Exception as e:
        logger.warning("fetch_stock_fund_flow_rank failed: %s", e)
        return None


def _fetch_stock_flow_raw(sort_by: str = "net_main", count: int = 50,
                           timeout: float = 10.0) -> list:
    fid_map = {"net_main": "f62", "net_ratio": "f184"}
    fid = fid_map.get(sort_by, "f62")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": str(count), "po": "1" if fid == "f62" else "0",
        "np": "1", "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": fid,
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f8,f37,f62,f66,f184,f21",
        "_": str(int(time.time() * 1000)),
    }
    resp = _http.get(url, params=params, timeout=timeout,
                   verify=False, headers=EASTMONEY_HEADERS)
    resp.raise_for_status()
    items = resp.json().get("data", {}).get("diff", [])
    stocks = []
    for item in items:
        code = item.get("f12", "")
        name = item.get("f14", "")
        if not code or not name:
            continue
        stocks.append({
            "code": code, "name": name,
            "price": item.get("f2"), "pct_chg": item.get("f3"),
            "turnover_rate": item.get("f8"), "volume_ratio": item.get("f37"),
            "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
            "amp_ratio": item.get("f66"), "net_main_ratio": item.get("f184"),
            "market_cap": item.get("f21"),
        })
    return stocks


def fetch_dashboard_data(use_real: bool = True) -> dict:
    if not use_real:
        return generate_mock_dashboard_data()

    rank_df = fetch_rank_akshare()
    snapshot = fetch_all_sectors_snapshot()

    if rank_df is None and snapshot is None:
        logger.info("真实数据不可用")
        return {
            "date": TRADE_DATE,
            "time_label": "--:--",
            "time_index": 0,
            "total_times": 0,
            "minutes": [],
            "rank": [],
            "series": {},
            "is_trading": _is_trading_time(),
            "data_date": datetime.now().strftime("%Y-%m-%d"),
        }

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
        return {
            "date": TRADE_DATE, "time_label": "--:--", "time_index": 0,
            "total_times": 0, "minutes": [], "rank": [], "series": {},
            "is_trading": _is_trading_time(),
            "data_date": datetime.now().strftime("%Y-%m-%d"),
        }

    minutes = _build_trade_minutes()
    time_label = snapshot["time"] if snapshot else "15:00"

    rank_data = []
    series_data = {}
    for sec in top_sectors:
        name = sec["name"]
        value = sec["net_main"]
        ratio = sec.get("net_main_ratio", 0)
        color = SECTOR_COLORS.get(name, "#666666")
        rank_data.append({
            "name": name, "value": value, "color": color,
            "net_main_ratio": ratio,
        })
        n = len(minutes)
        values = [None] * (n - 1) + [value] if n > 0 else [value]
        ratio_values = [None] * (n - 1) + [ratio] if n > 0 else [ratio]
        series_data[name] = {
            "name": name, "color": color,
            "times": minutes, "values": values,
            "ratio_values": ratio_values,
        }

    rank_data.sort(key=lambda x: x["value"], reverse=True)

    return {
        "date": TRADE_DATE,
        "is_trading": _is_trading_time(),
        "time_label": time_label,
        "time_index": len(minutes) - 1 if minutes else 0,
        "total_times": len(minutes),
        "minutes": minutes,
        "rank": rank_data,
        "series": series_data,
        "is_trading": _is_trading_time(),
        "data_date": datetime.now().strftime("%Y-%m-%d"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_dashboard_data(use_real=False)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:500])
    print(f"\n... 共 {len(data['rank'])} 个板块, {data['total_times']} 个时间点")

# ── 模拟股票池（供 stocks/flow mock 模式使用）─────────────────
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

def _is_trading_time() -> bool:
    """判断当前是否在交易时段（9:30-15:00，仅工作日）。"""
    from datetime import datetime, date
    now = datetime.now()
    wd = date.today().weekday()
    if wd >= 5:
        return False
    if now.hour < 9 or now.hour >= 15:
        return False
    if now.hour == 9 and now.minute < 30:
        return False
    return True
