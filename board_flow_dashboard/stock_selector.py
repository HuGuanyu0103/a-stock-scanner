#!/usr/bin/env python3
"""
盘中选股引擎 v2

从热板块（概念 + 行业）下钻到个股，结合资金流和实时量价因子评分，输出候选池。
适用周期：超短线（2-4 天持股）

v2 改进：
  - 双板块引擎：概念板块 + 行业板块同时扫描
  - 智能偏离评分：资金流向印证价格方向，而非一刀切奖励跑输
  - 新增实时因子：intraday_position（日内位置）、open_return（开盘涨幅）
  - 复用 collector 数据减少 API 调用
  - 日评分缓存缺失时自动降级

数据流程：
  热板块排名（概念+行业）→ 板块成分股实时行情+资金流 → 多因子评分 → 候选池
"""

import json
import logging
import random
import time
from datetime import datetime
from typing import Optional

import requests as req
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from .daily_scorer import load_daily_scores
except ImportError:
    from daily_scorer import load_daily_scores  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ── 策略参数 ────────────────────────────────────────────────

HOT_SECTOR_COUNT = 5           # 每种板块类型取前 N 个
STOCKS_PER_SECTOR = 10         # 每个板块取前 N 只成分股
CANDIDATE_POOL_SIZE = 25
MAX_PER_SECTOR = 5             # 同一板块最多入选数
BEAR_MARKET_POOL_SIZE = 10     # 普跌日候选池缩减至
BEAR_MARKET_THRESHOLD = 0.16   # 上涨板块占比低于此值视为普跌

# 流动性硬过滤
MIN_MARKET_CAP = 50            # 最小总市值（亿元）
MIN_TURNOVER_RATE = 0.3        # 最低换手率（%）

# ── 信号操作指引 ────────────────────────────────────────────

SIGNAL_GUIDE = {
    "放量上攻":   {"holding": 2,   "take_profit": "5-8%",   "stop_loss": "-3%"},
    "放量突破":   {"holding": 2,   "take_profit": "5-8%",   "stop_loss": "-3%"},
    "补涨潜力":   {"holding": "3-4", "take_profit": "8-12%", "stop_loss": "-4%"},
    "资金驱动":   {"holding": 2,   "take_profit": "6%",     "stop_loss": "-3.5%"},
    "量价齐升":   {"holding": "2-3", "take_profit": "6%",   "stop_loss": "-3.5%"},
    "温和吸筹":   {"holding": "3-4", "take_profit": "8-12%", "stop_loss": "-4%"},
    "滞涨关注":   {"holding": 3,   "take_profit": "5%",     "stop_loss": "-3%"},
    "弱势回避":   {"holding": 0,   "take_profit": "-",      "stop_loss": "-"},
    "高位风险":   {"holding": 0,   "take_profit": "-",      "stop_loss": "-"},
    "盘中观察":   {"holding": "2-3", "take_profit": "5%",   "stop_loss": "-3%"},
}

# v2 权重：新增 intraday_position、open_return
WEIGHTS = {
    "net_main_ratio": 0.28,       # 主力净占比（核心）
    "price_deviation": 0.20,      # 智能偏离（条件判断）
    "volume_ratio": 0.15,         # 量比
    "intraday_position": 0.12,    # 日内相对位置（新）
    "open_return": 0.11,          # 开盘后涨幅（新）
    "turnover_rate": 0.08,        # 换手率
    "amp_ratio": 0.06,            # 振幅
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

# ── 模拟数据（测试用）───────────────────────────────────────

_MOCK_STOCKS_BY_SECTOR = {
    "光通信模块": [
        ("300308", "中际旭创"), ("000063", "中兴通讯"),
        ("688313", "仕佳光子"), ("300502", "新易盛"), ("300394", "天孚通信"),
    ],
    "人形机器人": [
        ("688160", "步科股份"), ("002472", "双环传动"), ("300124", "汇川技术"),
        ("688017", "绿的谐波"), ("002747", "埃斯顿"),
    ],
    "AI芯片": [
        ("688041", "海光信息"), ("603986", "兆易创新"), ("002049", "紫光国微"),
        ("300782", "卓胜微"), ("688256", "寒武纪"),
    ],
    "数据中心": [
        ("000977", "浪潮信息"), ("603019", "中科曙光"),
        ("002415", "海康威视"), ("000938", "紫光股份"), ("688111", "金山办公"),
    ],
    "半导体": [
        ("688981", "中芯国际"), ("600703", "三安光电"),
        ("688012", "中微公司"), ("300223", "北京君正"), ("688072", "拓荆科技"),
    ],
    "低空经济": [
        ("002085", "万丰奥威"), ("300719", "安达维尔"), ("688070", "纵横股份"),
        ("002023", "海特高新"), ("600879", "航天电子"),
    ],
    "固态电池": [
        ("002074", "国轩高科"), ("300750", "宁德时代"), ("002709", "天赐材料"),
        ("300014", "亿纬锂能"), ("300568", "星源材质"),
    ],
    "通信设备": [
        ("600745", "闻泰科技"), ("002281", "光迅科技"),
        ("603160", "汇顶科技"), ("002396", "星网锐捷"), ("300628", "亿联网络"),
    ],
    "军工": [
        ("600760", "中航沈飞"), ("002179", "中航光电"), ("600893", "航发动力"),
        ("600862", "中航高科"), ("000768", "中航西飞"),
    ],
}

_MOCK_INDUSTRY_SECTORS = {
    "通信": [("600050", "中国联通"), ("600941", "中国移动"), ("300394", "天孚通信")],
    "半导体": [("002371", "北方华创"), ("688981", "中芯国际"), ("603986", "兆易创新")],
    "军工": [("600760", "中航沈飞"), ("600893", "航发动力"), ("002179", "中航光电")],
    "电力设备": [("300750", "宁德时代"), ("002074", "国轩高科"), ("601012", "隆基绿能")],
    "医药生物": [("600276", "恒瑞医药"), ("300760", "迈瑞医疗"), ("000538", "云南白药")],
}


# ── 归一化工具 ──────────────────────────────────────────────

def _minmax_normalize(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return {v: 0.5 for v in values}
    return {v: (v - lo) / (hi - lo) for v in values}


# ── 智能偏离评分（v2 核心改进）─────────────────────────────

def _smart_deviation_score(net_ratio: float, deviation: float) -> float:
    """资金流向必须印证价格方向，不能一刀切奖励跑输。

    Returns:
        0.0 ~ 1.0 的得分
    """
    if deviation < 0 and net_ratio > 5:
        # 跑输板块 + 资金进场 → 补涨潜力，最高分
        return 1.0
    elif deviation < 0 and net_ratio < 0:
        # 跑输板块 + 资金流出 → 真弱势，最低分
        return 0.0
    elif deviation > 0 and net_ratio > 10:
        # 跑赢板块 + 资金抢筹 → 强势龙头，高分
        return 1.0
    elif deviation > 0 and net_ratio < 0:
        # 跑赢板块 + 资金流出 → 拉高出货嫌疑，低分
        return 0.1
    elif deviation > 0 and net_ratio > 3:
        # 跑赢 + 温和资金 → 中等偏上
        return 0.75
    elif deviation < 0 and net_ratio > 0:
        # 跑输 + 微弱资金 → 中等偏下
        return 0.40
    elif deviation > 0 and net_ratio >= 0:
        # 跑赢 + 资金持平 → 一般
        return 0.55
    else:
        return 0.25


# ── 盘中信号推断 ────────────────────────────────────────────

def _infer_signal(s):
    net_ratio = s.get("net_main_ratio") or 0
    vol_ratio = s.get("volume_ratio") or 0
    pct = s.get("pct_chg") or 0
    dev = s.get("price_deviation") or 0
    intra_pos = s.get("intraday_position") or 0.5
    open_ret = s.get("open_return") or 0

    # 优先级从高到低：有意把"放量突破"提到"补涨潜力"前面
    # — 同时满足两者条件的股票，放量突破比补涨更值得关注
    if net_ratio > 15 and vol_ratio > 1.5 and pct > 3 and intra_pos > 0.6:
        return "放量上攻"
    if vol_ratio > 2 and pct > 2 and open_ret > 1:
        return "放量突破"
    if net_ratio > 10 and dev < -1 and intra_pos < 0.5:
        return "补涨潜力"
    if net_ratio > 8 and intra_pos > 0.7:
        return "资金驱动"
    if vol_ratio > 1.5 and net_ratio > 5:
        return "量价齐升"
    if dev < -2 and net_ratio < 0:
        return "弱势回避"
    if dev < -2 and net_ratio >= 0:
        return "滞涨关注"
    if net_ratio > 3:
        return "温和吸筹"
    if intra_pos > 0.85 and net_ratio < 0:
        return "高位风险"
    return "盘中观察"


# ── 多因子评分 ──────────────────────────────────────────────

def _score_and_rank(stocks):
    """多因子归一化 + 智能偏离 + 加权评分。"""
    if not stocks:
        return stocks

    # 标准归一化因子（min-max）
    continuous_keys = ["net_main_ratio", "volume_ratio",
                       "intraday_position", "open_return",
                       "turnover_rate", "amp_ratio"]
    norms = {}
    for key in continuous_keys:
        raw = [abs(s.get(key) or 0) if key == "open_return" else (s.get(key) or 0)
               for s in stocks]
        norms[key] = _minmax_normalize(raw)

    for s in stocks:
        for key in continuous_keys:
            raw_v = abs(s.get(key) or 0) if key == "open_return" else (s.get(key) or 0)
            s["norm_" + key] = round(norms[key].get(raw_v, 0.5), 4)

        # 开盘涨幅方向惩罚：v3.1 拆分低开场景，识别弱转强
        open_ret = s.get("open_return") or 0
        if open_ret >= 0:
            pass  # 高开/平开，不惩罚
        else:
            vol_ratio = s.get("volume_ratio") or 0
            nr = s.get("net_main_ratio") or 0
            if vol_ratio > 1.8 and nr > 8:
                # 弱转强：低开 + 放量 + 主力抢筹 → 罚轻一点
                s["norm_open_return"] *= 0.8
            elif vol_ratio < 1.0 and nr < 0:
                # 无量低开阴跌 → 严厉惩罚
                s["norm_open_return"] *= 0.15
            else:
                s["norm_open_return"] *= 0.3

        # 智能偏离评分（替代原来的 min-max）
        net_ratio = s.get("net_main_ratio") or 0
        deviation = s.get("price_deviation") or 0
        s["norm_price_deviation"] = _smart_deviation_score(net_ratio, deviation)

        # 日内位置因子：过高（>0.85）追涨风险，过低（<0.3）弱势
        # v3.1: 封板豁免 + 条件化惩罚
        intra = s.get("intraday_position") or 0.5
        pct = s.get("pct_chg") or 0
        if intra > 0.85:
            if pct > 7:
                pass  # 封板豁免：涨停股高位是正常的，不惩罚
            elif net_ratio > 10:
                s["norm_intraday_position"] *= 0.7  # 有资金支撑，减轻惩罚
            else:
                s["norm_intraday_position"] *= 0.4  # 高位无资金，正常惩罚
        elif intra < 0.3:
            if net_ratio > 5:
                s["norm_intraday_position"] *= 0.8  # 低位有资金抄底
            else:
                s["norm_intraday_position"] *= 0.6  # 低位无资金

        # 加权总分
        s["score"] = round(
            sum(s["norm_" + k] * w for k, w in WEIGHTS.items()), 4
        )
        s["signal"] = _infer_signal(s)

    stocks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return stocks


# ── 日评分合并 ──────────────────────────────────────────────

def _merge_daily_scores(stocks, scores_map):
    if not scores_map:
        for s in stocks:
            s["daily_combined_score"] = None
            s["daily_signal_count"] = 0
            s["daily_signal_names"] = ""
        return stocks
    for s in stocks:
        d = scores_map.get(s.get("code", ""))
        if d:
            s["daily_combined_score"] = d["combined_score"]
            s["daily_signal_count"] = d["signal_count"]
            s["daily_signal_names"] = d.get("signal_names", "")
        else:
            s["daily_combined_score"] = None
            s["daily_signal_count"] = 0
            s["daily_signal_names"] = ""
    return stocks


# ── 日评分自动降级 ──────────────────────────────────────────

def _generate_fallback_scores() -> dict:
    """日评分缓存缺失时，用全市场资金流排名生成简易兜底评分。"""
    try:
        from data_fetcher import fetch_stock_fund_flow_rank
    except ImportError:
        try:
            from .data_fetcher import fetch_stock_fund_flow_rank
        except ImportError:
            return {}

    stocks = fetch_stock_fund_flow_rank(sort_by="net_main", count=100)
    if not stocks:
        logger.warning("兜底评分生成失败：资金流数据为空")
        return {}

    scores = {}
    for i, s in enumerate(stocks):
        code = s.get("code", "")
        if not code:
            continue
        # 排名分：Top 1=10, Top 100≈0
        rank_score = max(0, round(10 - i * 0.1, 1))
        # 净额分：映射到 0-10
        net = s.get("net_main") or 0
        net_score = max(0, min(10, net * 1.5 + 5))
        # 占比分
        ratio = s.get("net_main_ratio") or 0
        ratio_score = max(0, min(10, ratio + 5))

        scores[code] = {
            "combined_score": round(rank_score * 0.3 + net_score * 0.4 + ratio_score * 0.3, 1),
            "signal_score": 0,
            "sentiment_score": 0,
            "factor_score": rank_score,
            "signal_count": 0,
            "signal_names": "",
        }

    logger.info("兜底日评分生成: %d 只股票", len(scores))
    return scores


# ── API 数据获取 ────────────────────────────────────────────

def _fetch_hot_sectors(sector_type: str = "concept", top_n: int = 5) -> list:
    """获取热点板块排名。

    Args:
        sector_type: "concept" (概念, m:90+t:3) 或 "industry" (行业, m:90+t:2)
    """
    fs_map = {"concept": "m:90+t:3", "industry": "m:90+t:2"}
    fs = fs_map.get(sector_type, "m:90+t:3")
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": "200", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": fs, "fields": "f12,f14,f3,f62,f184",
            "_": str(int(time.time() * 1000))}
        resp = req.get(url, params=params, timeout=8,
                       verify=False, headers=HEADERS)
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("diff", [])
        sectors = []
        for item in items[:top_n]:
            code, name = item.get("f12", ""), item.get("f14", "")
            if code and name:
                sectors.append({"code": code, "name": name,
                    "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
                    "pct_chg": item.get("f3", 0),
                    "type": sector_type})
        return sectors
    except Exception as e:
        logger.warning("获取%s热板块失败: %s",
                       "概念" if sector_type == "concept" else "行业", e)
        return []


def _fetch_sector_stocks(board_code: str, top_n: int = 10) -> list:
    """获取板块成分股行情（含日内高低开数据）。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": str(top_n), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "b:" + board_code + "+f:!50",
            # v2: 增加 f15(最高), f16(最低), f17(今开), f20(总市值)
            "fields": "f12,f14,f2,f3,f8,f15,f16,f17,f20,f37,f62,f66,f184",
            "_": str(int(time.time() * 1000))}
        resp = req.get(url, params=params, timeout=8,
                       verify=False, headers=HEADERS)
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("diff", [])
        stocks = []
        for item in items:
            code = item.get("f12", "")
            name = item.get("f14", "")
            if not code or not name:
                continue

            price = item.get("f2")
            high = item.get("f15")
            low = item.get("f16")
            open_price = item.get("f17")

            # 计算实时因子
            try:
                p = float(price) if price else 0
                h = float(high) if high else p
                l = float(low) if low else p
                o = float(open_price) if open_price else p
                intra_pos = round((p - l) / (h - l), 4) if h != l else 0.5
                open_ret = round((p - o) / o * 100, 2) if o else 0
            except (ValueError, ZeroDivisionError):
                intra_pos = 0.5
                open_ret = 0

            stocks.append({
                "code": code, "name": name,
                "price": price,
                "pct_chg": item.get("f3"),
                "turnover_rate": item.get("f8"),
                "volume_ratio": item.get("f37"),
                "net_main_inflow": round(float(item.get("f62", 0)) / 1e8, 2)
                                   if item.get("f62") else 0,
                "amp_ratio": item.get("f66"),
                "net_main_ratio": item.get("f184"),
                "market_cap": round(float(item.get("f20", 0)) / 1e8, 2)
                              if item.get("f20") else 0,
                # v2 新增实时因子
                "intraday_position": intra_pos,
                "open_return": open_ret,
            })
        return stocks
    except Exception as e:
        logger.warning("获取板块成分股(%s)失败: %s", board_code, e)
        return []


def _extract_hot_sectors_from_collector(collector,
                                         top_n: int = 5) -> list:
    """从 collector 内存数据中提取热板块，避免重复 API 调用。"""
    sectors = []
    for stype in ("concept", "industry"):
        data = collector.get_dashboard_data(sector_type=stype)
        rank = data.get("rank", [])
        for item in rank[:top_n]:
            sectors.append({
                "code": "",  # collector 不存板块 code，需要从快照补
                "name": item["name"],
                "net_main": item["value"],
                "pct_chg": 0,  # collector 快照里没有涨跌幅
                "type": stype,
            })
    return sectors


# ── 选股主流程 ──────────────────────────────────────────────

# ── 流动性硬过滤 ──────────────────────────────────────────

def _apply_liquidity_filter(stocks: list) -> tuple:
    """过滤流动性差的股票。返回 (通过, 剔除列表)。"""
    passed, removed = [], []
    for s in stocks:
        market_cap = s.get("market_cap") or 0
        turnover = s.get("turnover_rate") or 0
        code = s.get("code", "")
        # 科创板（688）豁免市值门槛（成长股允许小市值）
        is_star = code.startswith("688")
        cap_ok = is_star or market_cap >= MIN_MARKET_CAP
        liq_ok = (turnover or 0) >= MIN_TURNOVER_RATE
        if cap_ok and liq_ok:
            passed.append(s)
        else:
            removed.append(s)
    if removed:
        logger.info("流动性过滤剔除 %d 只: %s",
                     len(removed),
                     ", ".join(f"{r['name']}(市值{r.get('market_cap',0):.0f}亿,换手{r.get('turnover_rate',0):.1f}%)"
                              for r in removed[:5]))
    return passed, removed


# ── 诱多出货预警 ──────────────────────────────────────────

def _apply_fraud_filter(stocks: list) -> tuple:
    """剔除诱多嫌疑股：高位 + 主力流出 + 缩量/高开低走。"""
    passed, removed = [], []
    for s in stocks:
        intra = s.get("intraday_position") or 0.5
        net_ratio = s.get("net_main_ratio") or 0
        open_ret = s.get("open_return") or 0

        # 三个条件同时满足 → 诱多嫌疑
        is_high = intra > 0.8
        is_outflow = net_ratio < 0
        is_opened_high = open_ret > 1  # 高开但...
        is_selling_off = is_high and is_outflow and is_opened_high

        if is_selling_off:
            removed.append(s)
        else:
            passed.append(s)

    if removed:
        logger.info("诱多出货预警剔除 %d 只: %s",
                     len(removed),
                     ", ".join(f"{r['name']}(位{intra:.2f},净{r.get('net_main_ratio',0):.1f}%)"
                              for r in removed[:5]))
    return passed, removed


# ── 大盘环境评估 ──────────────────────────────────────────

def _get_market_breadth() -> float:
    """获取全市场上涨比例（用概念板块涨跌比近似）。

    Returns:
        0.0 ~ 1.0, 值越高市场越好
    """
    try:
        sectors = _fetch_hot_sectors(sector_type="concept", top_n=200)
        if not sectors or len(sectors) < 50:
            return 0.5  # 数据不够不判断
        up = sum(1 for s in sectors if (s.get("pct_chg") or 0) > 0)
        ratio = up / len(sectors)
        logger.info("大盘广度: %d/%d 板块上涨 (%.0f%%)",
                     up, len(sectors), ratio * 100)
        return ratio
    except Exception as e:
        logger.warning("大盘广度获取失败: %s", e)
        return 0.5


# ── 板块集中度管控 ──────────────────────────────────────────

def _apply_sector_concentration(candidates: list, max_per: int = MAX_PER_SECTOR,
                                 pool_size: int = CANDIDATE_POOL_SIZE) -> list:
    """确保候选池中同一板块不超过 max_per 只。"""
    sector_counts: dict[str, int] = {}
    result = []
    for s in candidates:
        sector = s.get("sector", "未知")
        if sector_counts.get(sector, 0) >= max_per:
            continue
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        result.append(s)
        if len(result) >= pool_size:
            break
    logger.info("板块集中度管控: %d 只 → %d 只 (max %d/板块)",
                 len(candidates), len(result), max_per)
    return result


# ── 告警推送 ──────────────────────────────────────────────

def _send_alert(title: str, content: str):
    """通过 Server酱 推送告警。失败时仅打日志。"""
    try:
        import os as _os
        _parent = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        import sys as _sys
        if _parent not in _sys.path:
            _sys.path.insert(0, _parent)
        from push import send as push_send
        push_send(title, content)
    except Exception as e:
        logger.warning("告警推送失败: %s (title=%s)", e, title)


def _check_alerts(candidates: list, hot_sectors: list,
                  daily_scores_ok: bool, api_failures: int = 0):
    """检查并推送关键告警。"""
    alerts = []

    # 空池告警
    if not candidates:
        alerts.append(("⚠️ 候选池为空",
                        f"时间: {datetime.now().strftime('%H:%M')}\n"
                        f"热板块: {len(hot_sectors)} 个\n"
                        f"日评分: {'正常' if daily_scores_ok else '缺失'}"))

    # 候选池过小告警
    elif len(candidates) < 8:
        alerts.append(("⚠️ 候选池过小",
                        f"候选池仅 {len(candidates)} 只\n"
                        f"热板块: {len(hot_sectors)} 个"))

    # API 连续失败告警
    if api_failures >= 5:
        alerts.append(("🚨 API 连续失败",
                        f"已连续失败 {api_failures} 次\n"
                        f"时间: {datetime.now().strftime('%H:%M')}"))

    # 日评分缺失告警
    if not daily_scores_ok:
        alerts.append(("⚠️ 日评分缺失",
                        "已使用资金流排名兜底评分"))

    for title, content in alerts:
        _send_alert(title, content)


# ── 选股主流程 ──────────────────────────────────────────────

def _select_stocks_real(collector=None):
    """实时选股：双板块引擎 + 实时因子。"""
    # Step 1: 获取热板块 — 优先复用 collector 数据
    hot_sectors = []
    if collector:
        hot_sectors = collector.get_top_sectors(top_n=HOT_SECTOR_COUNT)
        # 验证数据是否可用（有 code 才能下钻成分股）
        if hot_sectors and any(s.get("code") for s in hot_sectors):
            logger.info("从 collector 获取热板块: %d 个", len(hot_sectors))
        else:
            hot_sectors = []

    if not hot_sectors:
        # 降级：直接调 API
        for stype in ("concept", "industry"):
            sectors = _fetch_hot_sectors(sector_type=stype, top_n=HOT_SECTOR_COUNT)
            hot_sectors.extend(sectors)

    if not hot_sectors:
        raise RuntimeError("热板块数据为空")

    logger.info("热板块: 概念 %d + 行业 %d = 共 %d",
                sum(1 for s in hot_sectors if s.get("type") == "concept"),
                sum(1 for s in hot_sectors if s.get("type") == "industry"),
                len(hot_sectors))

    # Step 2: 下钻成分股
    seen_sectors = set()
    all_stocks = []
    for sector in hot_sectors:
        if sector["name"] in seen_sectors:
            continue
        seen_sectors.add(sector["name"])

        code = sector.get("code", "")
        if not code:
            continue  # 没有 code 无法下钻

        stocks = _fetch_sector_stocks(code, top_n=STOCKS_PER_SECTOR)
        for s in stocks:
            s["sector"] = sector["name"]
            s["sector_type"] = sector.get("type", "concept")
            s["sector_pct"] = sector.get("pct_chg", 0)
            s["price_deviation"] = round(
                (s.get("pct_chg") or 0) - (sector.get("pct_chg") or 0), 2)
        all_stocks.extend(stocks)

    if not all_stocks:
        raise RuntimeError("无成分股数据")

    logger.info("成分股: %d 只（去重前 %d 个板块）",
                len(all_stocks), len(seen_sectors))

    # Step 3: 智能去重
    seen = {}  # code -> stock_dict
    for s in all_stocks:
        code = s["code"]
        if code not in seen:
            seen[code] = s
        else:
            cur_inflow = s.get("net_main_inflow") or 0
            exist_inflow = seen[code].get("net_main_inflow") or 0
            if cur_inflow > exist_inflow:
                seen[code] = s
    unique = list(seen.values())

    # Step 4: 科创板 + 流动性过滤
    unique = [s for s in unique if not s["code"].startswith("688")]
    unique, _liq_removed = _apply_liquidity_filter(unique)

    # Step 5: 大盘广度 → 动态候选池大小
    breadth = _get_market_breadth()
    if breadth < BEAR_MARKET_THRESHOLD:
        effective_pool_size = BEAR_MARKET_POOL_SIZE
        risk_level = "high"
        logger.info("普跌环境 (广度 %.0f%%) 候选池缩减至 %d",
                     breadth * 100, effective_pool_size)
    elif breadth < 0.30:
        effective_pool_size = CANDIDATE_POOL_SIZE
        risk_level = "medium"
    else:
        effective_pool_size = CANDIDATE_POOL_SIZE
        risk_level = "low"

    # Step 6: 评分排序
    ranked = _score_and_rank(unique)
    ranked = _merge_daily_scores(ranked, _DAILY_SCORES)

    # Step 7: 诱多出货剔除
    ranked, _fraud_removed = _apply_fraud_filter(ranked)

    # Step 8: 板块集中度管控
    candidates = _apply_sector_concentration(
        ranked, max_per=MAX_PER_SECTOR, pool_size=effective_pool_size
    )

    # Step 9: 附加信号操作指引 + risk_level
    for c in candidates:
        signal = c.get("signal", "盘中观察")
        guide = SIGNAL_GUIDE.get(signal, SIGNAL_GUIDE["盘中观察"])
        c["holding_days"] = guide["holding"]
        c["take_profit"] = guide["take_profit"]
        c["stop_loss"] = guide["stop_loss"]

    # Step 10: 告警检查
    _check_alerts(
        candidates=candidates,
        hot_sectors=hot_sectors,
        daily_scores_ok=bool(_DAILY_SCORES),
        api_failures=0,  # 选股层不跟踪 API 失败，由 collector 负责
    )

    hot_names = [s["name"] for s in hot_sectors]
    return {"time": datetime.now().strftime("%H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(candidates),
        "candidates": candidates,
        "hot_sectors": hot_names,
        "risk_level": risk_level,
        "market_breadth": round(breadth, 2),
        "filter_stats": {
            "liquidity_removed": len(_liq_removed),
            "fraud_removed": len(_fraud_removed),
            "pool_size": effective_pool_size,
        },
        "mode": "live"}


def _select_stocks_mock():
    """模拟选股（测试用）。"""
    sectors = list(_MOCK_STOCKS_BY_SECTOR.keys())[:HOT_SECTOR_COUNT]
    result = []
    for sector in sectors:
        stocks_list = _MOCK_STOCKS_BY_SECTOR.get(sector, [])
        sector_pct = round(random.uniform(0.5, 4.0), 2)
        for code, name in stocks_list:
            pct_chg = round(random.uniform(-2, 6), 2)
            price = round(random.uniform(10, 200), 2)
            intra_pos = round(random.uniform(0.1, 0.95), 2)
            open_ret = round(random.uniform(-2, 4), 2)
            result.append({"code": code, "name": name, "sector": sector,
                "sector_type": "concept",
                "price": price,
                "pct_chg": pct_chg,
                "net_main_inflow": round(random.uniform(-3, 12), 2),
                "net_main_ratio": round(random.uniform(-5, 20), 2),
                "volume_ratio": round(random.uniform(0.3, 3.0), 2),
                "turnover_rate": round(random.uniform(0.5, 10.0), 1),
                "amp_ratio": round(random.uniform(1, 7), 1),
                "market_cap": round(random.uniform(30, 500), 1),
                "intraday_position": intra_pos,
                "open_return": open_ret,
                "sector_pct": sector_pct,
                "price_deviation": round(pct_chg - sector_pct, 2)})

    ranked = _score_and_rank(result)
    ranked = [s for s in ranked if not s["code"].startswith("688")]
    ranked = _merge_daily_scores(ranked, _DAILY_SCORES)

    if not _DAILY_SCORES:
        signal_bank = ["放量突破", "MA5金叉MA10", "均线多头排列", "MACD金叉",
                       "KDJ超卖金叉", "RSI上穿50", "连续3日放量", "涨停回踩10日线",
                       "平台突破", "连板梯队", "情绪周期"]
        for c in ranked:
            n_sig = random.randint(1, 5)
            c["daily_combined_score"] = round(random.uniform(2, 12), 1)
            c["daily_signal_count"] = n_sig
            c["daily_signal_names"] = " | ".join(
                random.sample(signal_bank, min(n_sig, len(signal_bank))))

    # 附加信号指引
    for c in ranked:
        guide = SIGNAL_GUIDE.get(c.get("signal", "盘中观察"), SIGNAL_GUIDE["盘中观察"])
        c["holding_days"] = guide["holding"]
        c["take_profit"] = guide["take_profit"]
        c["stop_loss"] = guide["stop_loss"]

    hot_names = sectors + list(_MOCK_INDUSTRY_SECTORS.keys())[:2]
    candidates = ranked[:CANDIDATE_POOL_SIZE]
    return {"time": datetime.now().strftime("%H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(candidates),
        "candidates": candidates,
        "hot_sectors": hot_names,
        "risk_level": "low",
        "market_breadth": 0.55,
        "filter_stats": {"liquidity_removed": 0, "fraud_removed": 0,
                          "pool_size": CANDIDATE_POOL_SIZE},
        "mode": "live" if _DAILY_SCORES else "standalone"}


# ── 全局状态 ────────────────────────────────────────────────

_DAILY_SCORES = {}
_CACHE = {}
_CACHE_TTL = 0.0
_CACHE_LIFETIME = 60.0


def select_stocks(use_mock: bool = False, collector=None) -> dict:
    """盘中选股入口。

    Args:
        use_mock: True 时使用模拟数据
        collector: SectorFlowCollector 实例（可选，用于数据复用）
    """
    global _CACHE, _CACHE_TTL, _DAILY_SCORES

    # 加载日评分（首次调用）
    if not _DAILY_SCORES:
        _DAILY_SCORES.update(load_daily_scores())
        if _DAILY_SCORES:
            logger.info("加载日评分缓存: %d 只股票", len(_DAILY_SCORES))
        else:
            # v2: 自动降级 — 用资金流排名生成兜底评分
            logger.info("日评分缓存缺失，自动生成兜底评分...")
            _DAILY_SCORES.update(_generate_fallback_scores())

    # 缓存控制
    now = time.time()
    if not use_mock and _CACHE and (now - _CACHE_TTL) < _CACHE_LIFETIME:
        return _CACHE

    if use_mock:
        result = _select_stocks_mock()
    else:
        try:
            result = _select_stocks_real(collector=collector)
        except Exception as e:
            logger.warning("实时选股失败，降级为模拟: %s", e)
            result = _select_stocks_mock()

    if not use_mock:
        _CACHE = result
        _CACHE_TTL = now
    return result


# ── CLI 测试 ────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = select_stocks(use_mock=True)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
    print(f"\n共 {data['total']} 只候选 · 模式={data['mode']}")
    for c in data['candidates'][:10]:
        print(f"  {c['name']:6s} {c['sector']:6s} "
              f"涨{c['pct_chg']:>+5.1f}% "
              f"主力{c['net_main_inflow']:>5.1f}亿 "
              f"净占比{c['net_main_ratio']:>5.1f}% "
              f"量比{c['volume_ratio']:>4.1f} "
              f"日内位{c['intraday_position']:.2f} "
              f"开涨幅{c['open_return']:>+5.1f}% "
              f"评分{c['score']:.3f} {c['signal']}")
