#!/usr/bin/env python3
"""
盘中选股引擎 v3

双轨并行：A 池（当日热力追涨）+ B 池（周期回调低吸），各自独立输出 25 只。
适用周期：超短线（2-4 天持股）

v3 改进：
  - 双轨架构：A 池当日热力板块 + B 池周期回调板块，互补覆盖
  - B 池专属评分：回调板块跑赢/抗跌/跟跌分拆，取消低开惩罚，弱化低位惩罚
  - 3 个低吸信号：主线分歧低吸、板块洗盘承接、缩量止跌企稳
  - 板块集中度管控：A 池 max 5/板块，B 池 max 3/板块
  - 大盘环境动态调节双池权重（普跌时 B 池放大）

数据流程：
  A 池：热板块排名（概念+行业）→ 成分股行情+资金流 → 7因子评分 → 候选池
  B 池：sector_reviewer 回调板块 → 成分股行情+资金流 → 修正评分 → 候选池
  合并：各自输出 25 只，前端独立展示
"""

import bisect
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests as req
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 共享 Session，绕过 macOS 系统代理
_http = req.Session()
_http.trust_env = False

try:
    from .daily_scorer import load_daily_scores
    from .sector_reviewer import SectorReviewer, PULLBACK_MAX_SECTORS
    from .data_fetcher import _now_time
except ImportError:
    from daily_scorer import load_daily_scores  # type: ignore[no-redef]
    from sector_reviewer import SectorReviewer, PULLBACK_MAX_SECTORS  # type: ignore[no-redef]
    from data_fetcher import _now_time  # type: ignore[no-redef]

# signals 模块的函数在 _select_stocks_real 内按需导入，避免循环依赖

logger = logging.getLogger(__name__)

# ── 策略参数 ────────────────────────────────────────────────

HOT_SECTOR_COUNT = 5           # 资金流维度：每种板块类型取前 N 个
HOT_SECTOR_COUNT_PCT = 3       # 价格动量维度：每种板块额外取前 N 个（涨幅最大）
STOCKS_PER_SECTOR = 50         # 每个板块取前 N 只成分股（v4.0 放宽至 50）
CANDIDATE_POOL_SIZE = 50       # 候选池总容量（= A + B）
DAILY_SCORE_WEIGHT = 0.25      # 日评分在最终排名中的权重
MAX_PER_SECTOR = 8             # 同一板块最多入选数（纯主板放宽）
MAX_PER_SECTOR_B = 8           # B 池同一板块最多入选数（纯主板放宽）
MAX_STOCKS_PER_MEGA_SECTOR = 10  # 同一大赛道总上限（TODO Phase 2: 需先建 sector→mega_sector 映射表才能生效）
BEAR_MARKET_POOL_SIZE = 10     # 普跌日候选池缩减至（已废弃，保留兼容）
BEAR_MARKET_THRESHOLD = 0.16   # 上涨板块占比低于此值视为普跌
EXTREME_BEAR_THRESHOLD = 0.08  # v4.0: 极端熊市阈值，A池彻底禁用

# ── v4.0: 三级系统性风控熔断参数 ─────────────────────────
SECTOR_MELTDOWN_FLOW = -20     # 板块主力净流出超此值(亿)触发熔断
SECTOR_MELTDOWN_PCT = -3.0     # 板块跌幅超此值(%)触发熔断
SECTOR_MELTDOWN_PENALTY = 0.5  # 熔断板块成分股得分乘以此系数
FLASH_CRASH_PCT = -5.0         # 个股盘中急跌超此值(%)直接剔除
FLASH_CRASH_OPEN_GAP = -4.0    # 开盘后跌幅超此值(%)判定急跌

# ── B 池（回调低吸）参数 ────────────────────────────────────

HOT_PULLBACK_RATIO_A = 25      # A 池名额
HOT_PULLBACK_RATIO_B = 25      # B 池名额

# B 池额外加成（系数，非绝对分）
PULLBACK_BONUS_MULTIPLIER = 1.10  # B 池得分 ×1.10

# B 池大盘环境调节（已固定为 25，保留常量兼容）
BEAR_PULLBACK_A = 25           # 普跌时 A 池名额
BEAR_PULLBACK_B = 25           # 普跌时 B 池名额

# 流动性硬过滤（已改用流通市值 f21 替代总市值 f20）
MIN_MARKET_CAP_A = 30          # A 池最小流通市值（亿元）
MIN_MARKET_CAP_B = 20          # B 池最小流通市值（亿元，低吸需更宽选股面）
MIN_TURNOVER_RATE = 0.5        # 最低换手率（%，主板要求更高活跃度）

# ── 信号操作指引 ────────────────────────────────────────────

SIGNAL_GUIDE = {
    # A 池信号 — 纯主板 10cm 环境，止盈下调 1-2%
    "放量上攻":   {"holding": 2,   "take_profit": "4-6%",   "stop_loss": "-3%"},
    "放量突破":   {"holding": 2,   "take_profit": "4-6%",   "stop_loss": "-3%"},
    "补涨潜力":   {"holding": "3-4", "take_profit": "6-8%", "stop_loss": "-4%"},
    "资金驱动":   {"holding": 2,   "take_profit": "5%",     "stop_loss": "-3.5%"},
    "量价齐升":   {"holding": "2-3", "take_profit": "5%",   "stop_loss": "-3.5%"},
    "温和吸筹":   {"holding": "3-4", "take_profit": "6-8%", "stop_loss": "-4%"},
    "滞涨关注":   {"holding": 3,   "take_profit": "4%",     "stop_loss": "-3%"},
    "弱势回避":   {"holding": 0,   "take_profit": "-",      "stop_loss": "-"},
    "高位风险":   {"holding": 0,   "take_profit": "-",      "stop_loss": "-"},
    "盘中观察":   {"holding": "2-3", "take_profit": "4%",   "stop_loss": "-3%"},
    # B 池专属信号 — 纯主板无 20cm，暴利机会减少，止盈下调 3%
    "主线分歧低吸": {"holding": "3-4", "take_profit": "7-9%", "stop_loss": "-4.5%"},
    "板块洗盘承接": {"holding": 3,   "take_profit": "6%",    "stop_loss": "-3.5%"},
    "缩量止跌企稳": {"holding": "3-4", "take_profit": "6-8%", "stop_loss": "-4%"},
}

# ── v4.0: ATR 动态止盈止损 ────────────────────────────────────

def _estimate_atr_stops(stock: dict) -> dict:
    """基于日内振幅估算 ATR 动态止盈止损位。

    以振幅作为当日波动率的代理指标，ATR ≈ 振幅 × 0.65。
    止损 = 1.5×ATR，止盈 = 2.5×ATR（均为百分比）。

    未来可从 daily_scorer 的日K线数据中获取精确 ATR(14) 替代此估算。

    Returns:
        {"atr_stop_loss": str, "atr_take_profit": str}
    """
    amp = stock.get("amp_ratio") or 0
    if amp <= 0:
        # 无振幅数据时回退到信号默认值
        return {"atr_stop_loss": "-", "atr_take_profit": "-"}

    estimated_atr_pct = amp * 0.65  # ATR 通常约为振幅的 65%
    atr_stop = round(-1.5 * estimated_atr_pct, 1)
    atr_tp = round(2.5 * estimated_atr_pct, 1)

    return {
        "atr_stop_loss": f"{atr_stop}%",
        "atr_take_profit": f"{atr_tp}%",
    }

# v3.5 权重：量价为主(60%)，资金确认为辅(16%)，日评融合(25%，见 DAILY_SCORE_WEIGHT)
WEIGHTS = {
    "volume_ratio": 0.20,       # 量比 — 超短线量是王
    "price_deviation": 0.16,    # 智能偏离 — 相对板块强弱
    "net_main_ratio": 0.16,    # 主力净占比 — 确认信号（含流置信动态降权）
    "intraday_position": 0.12, # 日内相对位置 — 入场时机
    "open_return": 0.10,       # 开盘涨幅 — 开盘定多空
    "turnover_rate": 0.10,     # 换手率 — 流动性
    "amp_ratio": 0.08,         # 振幅 — 盈利空间
    "short_momentum": 0.06,    # 短期动量（近5日涨幅）— 趋势延续性
    "breakout_dist": 0.04,     # 突破距离（距20日高点）— 空间判断
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
        ("002371", "北方华创"), ("603986", "兆易创新"), ("002049", "紫光国微"),
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
    "新能源车": [
        ("002594", "比亚迪"), ("601238", "广汽集团"), ("600104", "上汽集团"),
        ("000625", "长安汽车"), ("601633", "长城汽车"),
    ],
    "光伏": [
        ("601012", "隆基绿能"), ("002459", "晶澳科技"), ("600438", "通威股份"),
        ("002129", "TCL中环"), ("601615", "明阳智能"),
    ],
    "消费电子": [
        ("002475", "立讯精密"), ("601138", "工业富联"), ("002241", "歌尔股份"),
        ("600183", "生益科技"), ("002456", "欧菲光"),
    ],
    "创新药": [
        ("600276", "恒瑞医药"), ("000538", "云南白药"), ("002007", "华兰生物"),
        ("600196", "复星医药"), ("000963", "华东医药"),
    ],
}

_MOCK_INDUSTRY_SECTORS = {
    "通信": [("600050", "中国联通"), ("600941", "中国移动"), ("300394", "天孚通信")],
    "半导体": [("002371", "北方华创"), ("688981", "中芯国际"), ("603986", "兆易创新")],
    "军工": [("600760", "中航沈飞"), ("600893", "航发动力"), ("002179", "中航光电")],
    "电力设备": [("300750", "宁德时代"), ("002074", "国轩高科"), ("601012", "隆基绿能")],
    "医药生物": [("600276", "恒瑞医药"), ("300760", "迈瑞医疗"), ("000538", "云南白药")],
    "汽车": [("002594", "比亚迪"), ("000625", "长安汽车"), ("601238", "广汽集团")],
    "银行": [("601398", "工商银行"), ("600036", "招商银行"), ("000001", "平安银行")],
}


# ── 归一化工具 ──────────────────────────────────────────────

def _minmax_normalize(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return {v: 0.5 for v in values}
    return {v: (v - lo) / (hi - lo) for v in values}


# ── 智能偏离评分（v2 核心改进）─────────────────────────────

def _smart_deviation_score(net_ratio: float, deviation: float,
                            volume_ratio: float = 1.0, pct_chg: float = 0) -> float:
    """资金-价格二元印证评分（v7.1 修正：加入量价时序维度）。

    v7.1 修正说明（评审反馈）：
      原版"跌了+主力进=补涨潜力(1.0)"忽视了一个关键风险——
      放量阴跌中主力流入可能是下跌中继（接飞刀），而非补涨。
      加入缩量/放量(volume_ratio)和急跌/阴跌(pct_chg)两个时间维度约束：
        - 缩量微跌 + 主力进 → 洗盘承接（0.9分）
        - 放量急跌 + 主力进 → 下跌中继风险（0.3分）

    Returns:
        0.0 ~ 1.0 的得分
    """
    # 核心维度判断
    is_shrink = volume_ratio < 0.8  # 缩量
    is_expand = volume_ratio > 1.5  # 放量
    is_sharp = pct_chg < -3        # 急跌
    is_gentle = -3 <= pct_chg < 0  # 阴跌/微跌

    if deviation < 0 and net_ratio > 5:
        # 跑输板块 + 资金进场
        if is_expand and is_sharp:
            # 放量急跌中主力流入 = 下跌中继风险（接飞刀）
            return 0.30
        elif is_shrink and is_gentle:
            # 缩量微跌中主力流入 = 洗盘承接，最佳低吸
            return 1.0
        elif is_shrink and is_sharp:
            # 缩量急跌中主力流入 = 恐慌性补跌，观望为主
            return 0.50
        elif is_expand:
            # 放量 + 主力流入 = 有承接但分歧大
            return 0.55
        else:
            # 温和放量/缩量 + 主力流入 = 补涨潜力
            return 0.80
    elif deviation < 0 and net_ratio < 0:
        return 0.0  # 跑输 + 资金流出 = 真弱势
    elif deviation > 0 and net_ratio > 10:
        return 1.0  # 跑赢 + 资金抢筹 = 强势龙头
    elif deviation > 0 and net_ratio < 0:
        return 0.1  # 跑赢 + 资金流出 = 拉高出货嫌疑
    elif deviation > 0 and net_ratio > 3:
        return 0.75  # 跑赢 + 温和资金 = 中等偏上
    elif deviation < 0 and net_ratio > 0:
        return 0.40  # 跑输 + 微弱资金 = 中等偏下
    elif deviation > 0 and net_ratio >= 0:
        return 0.55  # 跑赢 + 资金持平 = 一般
    else:
        return 0.25


# ── 资金流置信度（v3.4 新增）─────────────────────────────────

def _flow_confidence(net_ratio: float, pct_chg: float,
                     is_pullback: bool = False) -> float:
    """资金流数据的置信度乘数：价格与资金同向→可信，背离→降权。

    核心逻辑：主力净流入是统计推断（按订单大小分档），不是真实身份。
    价格涨 + 资金进 = 可信（大单确实在推价格）
    价格跌 + 资金进 = 可疑（大单统计可能被拆单粉饰）

    Returns:
        0.0 ~ 1.0 的置信度乘数
    """
    price_up = pct_chg > 0
    flow_in = net_ratio > 0

    if price_up == flow_in:
        return 1.0  # 同向：价格和资金互相印证

    # 背离：价格和资金方向相反
    mag = abs(net_ratio)

    if is_pullback:
        # B 池回调板块中，"股跌+资金进"可能是逆势承接，给中等置信
        if not price_up and flow_in and mag > 5:
            return 0.60
        elif not price_up and flow_in:
            return 0.35
        else:
            return 0.15
    else:
        # A 池热力板块中，背离=可疑
        if mag > 10:
            return 0.50  # 大资金背离，有可能性
        elif mag > 5:
            return 0.30
        else:
            return 0.10  # 小资金背离，极不可信

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


# ── B 池专属：回调板块信号推断 ─────────────────────────────

def _infer_signal_pullback(s):
    """B 池（回调低吸）信号推断 — 识别分歧低吸机会。"""
    net_ratio = s.get("net_main_ratio") or 0
    vol_ratio = s.get("volume_ratio") or 0
    pct = s.get("pct_chg") or 0
    dev = s.get("price_deviation") or 0
    intra_pos = s.get("intraday_position") or 0.5
    open_ret = s.get("open_return") or 0

    # P0: 主线分歧低吸 — 回调板块 + 主力逆势承接 + 日内低位
    if net_ratio > 10 and dev < 0 and intra_pos < 0.5 and pct < 2:
        return "主线分歧低吸"

    # P1: 板块洗盘承接 — 缩量分歧 + 主力持续流入
    if net_ratio > 5 and vol_ratio > 1.2 and dev > -3 and pct < 3:
        return "板块洗盘承接"

    # P2: 缩量止跌企稳 — 缩量 + 止跌迹象
    if vol_ratio < 1.2 and intra_pos > 0.3 and pct > -3 and open_ret < 1:
        return "缩量止跌企稳"

    # 无法明确判断时回退到通用信号
    return _infer_signal(s)


# ── B 池专属：回调板块偏离评分 ─────────────────────────────

def _smart_deviation_score_pullback(net_ratio: float, deviation: float) -> float:
    """B 池价格偏离评分 — 抗跌+资金承接 = 高分，跟跌+无资金 = 低分。

    与 A 池逻辑不同：回调板块中「跑输板块」是预期内的，关键是资金是否承接。
    """
    if deviation > 0 and net_ratio > 3:
        # 跑赢回调板块 + 资金流入 → 相对强势，高分
        return 0.85
    elif deviation >= -2 and net_ratio > 5:
        # 抗跌（跑输 < 2%）+ 资金承接 → 最佳低吸信号
        return 1.0
    elif deviation < -2 and net_ratio > 5:
        # 跟跌（跑输较大）+ 资金仍在承接 → 仍有价值
        return 0.55
    elif deviation < 0 and net_ratio > 0:
        # 微跌 + 微弱资金 → 一般
        return 0.40
    elif deviation > 0 and net_ratio < 0:
        # 跑赢 + 资金流出 → 诱多嫌疑
        return 0.15
    elif deviation < 0 and net_ratio < 0:
        # 跑输 + 资金流出 → 真弱势
        return 0.05
    elif deviation > 0 and net_ratio >= 0:
        # 跑赢 + 资金持平
        return 0.50
    else:
        return 0.25


# ── 多因子评分 ──────────────────────────────────────────────

def _score_and_rank(stocks, pool_type: str = "A"):
    """多因子归一化 + 智能偏离 + 加权评分。

    pool_type:
      "A" — 热力追涨池（原有逻辑）
      "B" — 回调低吸池（修正逻辑：取消低开惩罚、弱化低位惩罚、独立偏离评分）
    """
    if not stocks:
        return stocks

    is_pullback = (pool_type == "B")

    # 标准归一化因子（min-max）— open_return 不再 abs，正负含义相反
    continuous_keys = ["net_main_ratio", "volume_ratio",
                       "intraday_position", "open_return",
                       "turnover_rate", "amp_ratio",
                       "short_momentum", "breakout_dist"]
    norms = {}
    for key in continuous_keys:
        raw = [(s.get(key) or 0) for s in stocks]
        norms[key] = _minmax_normalize(raw)

    for s in stocks:
        for key in continuous_keys:
            raw_v = (s.get(key) or 0)
            s["norm_" + key] = round(norms[key].get(raw_v, 0.5), 4)

        # ── 开盘涨幅惩罚 ──────────────────────────────────
        open_ret = s.get("open_return") or 0

        if is_pullback:
            # B 池：回调板块低开是常态，取消惩罚
            pass  # ×1.0
        elif open_ret >= 0:
            pass  # A 池：高开/平开，不惩罚
        else:
            vol_ratio = s.get("volume_ratio") or 0
            nr = s.get("net_main_ratio") or 0
            if vol_ratio > 1.8 and nr > 8:
                s["norm_open_return"] *= 0.8   # 弱转强
            elif vol_ratio < 1.0 and nr < 0:
                s["norm_open_return"] *= 0.15  # 无量阴跌
            else:
                s["norm_open_return"] *= 0.3

        # ── 智能偏离评分 ──────────────────────────────────
        net_ratio = s.get("net_main_ratio") or 0
        deviation = s.get("price_deviation") or 0

        if is_pullback:
            s["norm_price_deviation"] = _smart_deviation_score_pullback(
                net_ratio, deviation)
        else:
            s["norm_price_deviation"] = _smart_deviation_score(net_ratio, deviation, s.get("volume_ratio") or 0, s.get("pct_chg") or 0)

        # ── 日内位置因子 ──────────────────────────────────
        intra = s.get("intraday_position") or 0.5
        pct = s.get("pct_chg") or 0

        if intra > 0.85:
            if pct > 7:
                pass  # 封板豁免
            elif is_pullback and net_ratio < 0:
                s["norm_intraday_position"] *= 0.4  # B 池高位+流出=严惩
            elif net_ratio > 10:
                s["norm_intraday_position"] *= 0.7
            else:
                s["norm_intraday_position"] *= 0.4
        elif intra < 0.3:
            if is_pullback and net_ratio > 5:
                s["norm_intraday_position"] *= 0.9  # B 池低位+资金=几乎不扣
            elif net_ratio > 5:
                s["norm_intraday_position"] *= 0.8  # A 池低位有资金
            else:
                s["norm_intraday_position"] *= 0.6  # 低位无资金

        # ── 加权总分（盘中因子）──────────────────────────
        # v3.4: 资金流置信度降权 + 日评分融合
        pct = s.get("pct_chg") or 0
        flow_conf = _flow_confidence(net_ratio, pct, is_pullback)

        effective_weights = dict(WEIGHTS)
        original_flow_w = effective_weights["net_main_ratio"]
        reduced_flow_w = original_flow_w * flow_conf
        redist = original_flow_w - reduced_flow_w  # 砍掉的权重分给其他因子
        effective_weights["net_main_ratio"] = reduced_flow_w

        other_keys = [k for k in WEIGHTS if k != "net_main_ratio"]
        other_total = sum(WEIGHTS[k] for k in other_keys)
        for k in other_keys:
            effective_weights[k] += redist * (WEIGHTS[k] / other_total)

        intraday_score = sum(s["norm_" + k] * w for k, w in effective_weights.items())

        # ── 日评分融合（全量百分位归一化）─────────────────
        daily_raw = s.get("daily_combined_score")
        if daily_raw is not None and _ALL_DAILY_SCORES_SORTED:
            # 在全量日评分中找百分位（0~1）
            rank = bisect.bisect_left(_ALL_DAILY_SCORES_SORTED, daily_raw)
            daily_norm = rank / len(_ALL_DAILY_SCORES_SORTED)
        elif daily_raw is not None:
            daily_norm = 0.5  # 无全量数据时中性
        else:
            daily_norm = 0.5  # 无日评分→中性

        s["daily_norm"] = round(daily_norm, 4)  # 供前端审计

        s["score"] = round(
            intraday_score * (1 - DAILY_SCORE_WEIGHT) + daily_norm * DAILY_SCORE_WEIGHT, 4
        )

        # v4.0: 资金流置信度直接乘总分，防止低置信度股票通过
        # 量比/换手率等因子绕过资金真实性风控
        if flow_conf <= 0.3:
            s["score"] = round(s["score"] * flow_conf, 4)
        s["flow_confidence"] = round(flow_conf, 2)

        # B 池额外加成（系数，非绝对分）
        if is_pullback:
            s["score"] = round(s["score"] * PULLBACK_BONUS_MULTIPLIER, 4)

        # ── 信号推断 ──────────────────────────────────────
        if is_pullback:
            s["signal"] = _infer_signal_pullback(s)
        else:
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
    """日评分缓存缺失时，用全市场资金流排名生成简易兜底评分。

    注意：此公式 (rank*0.3 + net*0.4 + ratio*0.3) 为经验值，未经严格回测校准。
    兜底评分仅用于保障系统在日评分缓存缺失时仍可运行，不应作为主要评分依据。
    TODO: 累积足够历史数据后，用回归校准权重与映射函数。
    """
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
        resp = _http.get(url, params=params, timeout=8,
                       verify=False, headers=HEADERS)
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("diff", [])
        sectors = []
        for item in items[:top_n]:
            code, name = item.get("f12", ""), item.get("f14", "")
            if code and name:
                sectors.append({"code": code, "name": name,
                    "net_main": round(float(item.get("f62", 0)) / 1e8, 2),
                    "net_main_ratio": item.get("f184", 0),
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
            # v4.0: f21(流通市值)替代f20(总市值)，避免总市值幻觉
            "fields": "f12,f14,f2,f3,f8,f15,f16,f17,f21,f37,f62,f66,f184",
            "_": str(int(time.time() * 1000))}
        resp = _http.get(url, params=params, timeout=8,
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
                "market_cap": round(float(item.get("f21", 0)) / 1e8, 2)
                              if item.get("f21") else 0,
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
                "code": item.get("code", ""),
                "name": item["name"],
                "net_main": item.get("net_main", item.get("value", 0)),
                "net_main_ratio": item.get("net_main_ratio", 0),
                "pct_chg": item.get("pct_chg", 0),
                "type": stype,
            })
    return sectors


# ── 选股主流程 ──────────────────────────────────────────────

# ── 流动性硬过滤 ──────────────────────────────────────────

def _apply_liquidity_filter(stocks: list, pool_type: str = "A") -> tuple:
    """过滤流动性差的股票。返回 (通过, 剔除列表)。

    Args:
        pool_type: "A" 使用 MIN_MARKET_CAP_A, "B" 使用 MIN_MARKET_CAP_B
    """
    min_cap = MIN_MARKET_CAP_B if pool_type == "B" else MIN_MARKET_CAP_A
    passed, removed = [], []
    for s in stocks:
        market_cap = s.get("market_cap") or 0
        turnover = s.get("turnover_rate") or 0
        cap_ok = market_cap >= min_cap
        liq_ok = (turnover or 0) >= MIN_TURNOVER_RATE
        if cap_ok and liq_ok:
            passed.append(s)
        else:
            removed.append(s)
    if removed:
        logger.info("流动性过滤[%s池]剔除 %d 只(市值<%d亿或换手<%.1f%%): %s",
                     pool_type, len(removed), min_cap, MIN_TURNOVER_RATE,
                     ", ".join(f"{r['name']}(市值{r.get('market_cap',0):.0f}亿,换手{r.get('turnover_rate',0):.1f}%)"
                              for r in removed[:5]))
    return passed, removed


# ── 涨停板过滤 ────────────────────────────────────────────

def _apply_limit_up_filter(stocks: list) -> tuple:
    """剔除已涨停的股票。

    涨停的股票当天无法买入，保留在候选池中没有意义。
    根据昨收价反推实际涨停价，而非简单用涨幅判断。
    """
    passed, removed = [], []
    for s in stocks:
        code = s.get("code", "")
        try:
            price = float(s.get("price", 0))
            pct = float(s.get("pct_chg", 0) or 0)
        except (ValueError, TypeError):
            passed.append(s)
            continue

        if price <= 0 or pct <= -100:
            passed.append(s)
            continue

        # 反推昨收价
        preclose = price / (1 + pct / 100)

        # 纯主板 10% 涨跌幅限制
        limit_price = round(preclose * 1.10, 2)

        # 当前价 >= 涨停价 → 已涨停（容忍 1 分钱四舍五入误差）
        if price >= limit_price - 0.01:
            removed.append(s)
        else:
            passed.append(s)

    if removed:
        logger.info("涨停板过滤剔除 %d 只: %s",
                     len(removed),
                     ", ".join(f"{r['name']}(价{r.get('price',0):.2f}/涨停{round(float(r.get('price',0))/(1+float(r.get('pct_chg',0) or 0)/100)*1.1,2):.2f})"
                              for r in removed[:10]))
    return passed, removed


# ── 诱多出货预警 ──────────────────────────────────────────

def _apply_fraud_filter(stocks: list) -> tuple:
    """剔除诱多嫌疑股：高位 + 主力流出 + 缩量/高开低走。"""
    passed, removed = [], []
    for s in stocks:
        intra = s.get("intraday_position") or 0.5
        net_ratio = s.get("net_main_ratio") or 0
        open_ret = s.get("open_return") or 0

        pct = s.get("pct_chg") or 0

        # 三个条件同时满足 → 诱多嫌疑
        is_high = intra > 0.8
        is_outflow = net_ratio < 0
        is_opened_high = open_ret > 1  # 高开但...
        is_selling_off = is_high and is_outflow and is_opened_high

        # v4.0: 极端背离 — 价格大涨 + 主力明显流出，直接剔除
        is_extreme_divergence = net_ratio < -5 and pct > 3

        if is_selling_off or is_extreme_divergence:
            removed.append(s)
        else:
            passed.append(s)

    if removed:
        logger.info("诱多出货预警剔除 %d 只: %s",
                     len(removed),
                     ", ".join(f"{r['name']}(位{intra:.2f},净{r.get('net_main_ratio',0):.1f}%)"
                              for r in removed[:5]))
    return passed, removed


# ── v4.0: 三级系统性风控熔断 ──────────────────────────────

def _apply_flash_crash_filter(stocks: list) -> tuple:
    """剔除盘中急跌个股：开盘后大幅下挫，可能有利空或踩踏。

    用已有数据近似：open_return < FLASH_CRASH_OPEN_GAP 且
    pct_chg < FLASH_CRASH_PCT 表示开盘后持续下跌，有闪崩嫌疑。
    """
    passed, removed = [], []
    for s in stocks:
        open_ret = s.get("open_return") or 0
        pct = s.get("pct_chg") or 0
        if open_ret < FLASH_CRASH_OPEN_GAP and pct < FLASH_CRASH_PCT:
            removed.append(s)
        else:
            passed.append(s)
    if removed:
        logger.info("急跌熔断剔除 %d 只: %s",
                     len(removed),
                     ", ".join(f"{r['name']}(开跌{r.get('open_return',0):+.1f}% 现跌{r.get('pct_chg',0):+.1f}%)"
                              for r in removed[:5]))
    return passed, removed


def _get_meltdown_sectors(hot_sectors: list) -> set:
    """识别触发板块熔断的板块名。

    条件：主力净流出 > SECTOR_MELTDOWN_FLOW 且 跌幅 > SECTOR_MELTDOWN_PCT。
    返回熔断板块名集合，这些板块的成分股将在评分中受惩罚。
    """
    meltdown = set()
    for s in hot_sectors:
        net_main = s.get("net_main", 0)
        pct = s.get("pct_chg", 0)
        if net_main < SECTOR_MELTDOWN_FLOW and pct < SECTOR_MELTDOWN_PCT:
            meltdown.add(s.get("name", ""))
    if meltdown:
        logger.info("板块熔断: %s", ", ".join(
            f"{n}" for n in meltdown))
    return meltdown


def _apply_sector_meltdown_penalty(stocks: list, meltdown_sectors: set) -> None:
    """对熔断板块成分股施加得分惩罚。"""
    if not meltdown_sectors:
        return
    count = 0
    for s in stocks:
        if s.get("sector", "") in meltdown_sectors:
            s["score"] = round(s["score"] * SECTOR_MELTDOWN_PENALTY, 4)
            s["meltdown_penalty"] = True
            count += 1
    if count:
        logger.info("板块熔断惩罚: %d 只成分股得分 ×%.1f", count,
                     SECTOR_MELTDOWN_PENALTY)


# ── 大盘环境评估 ──────────────────────────────────────────

def _get_market_breadth() -> float:
    """获取全市场上涨比例（概念板块 + 行业板块涨跌比）。

    Returns:
        0.0 ~ 1.0, 值越高市场越好
    """
    try:
        all_sectors = []
        for stype in ("concept", "industry"):
            sectors = _fetch_hot_sectors(sector_type=stype, top_n=200)
            if sectors:
                all_sectors.extend(sectors)

        if not all_sectors or len(all_sectors) < 80:
            return 0.5  # 数据不够不判断

        up = sum(1 for s in all_sectors if (s.get("pct_chg") or 0) > 0)
        ratio = up / len(all_sectors)
        logger.info("大盘广度: %d/%d 板块上涨 (%.0f%%)",
                     up, len(all_sectors), ratio * 100)
        return ratio
    except Exception as e:
        logger.warning("大盘广度获取失败: %s", e)
        return 0.5


# ── 板块集中度管控 ──────────────────────────────────────────

def _apply_sector_concentration(candidates: list, max_per: int = MAX_PER_SECTOR,
                                 pool_size: int = CANDIDATE_POOL_SIZE) -> list:
    """确保候选池中同一板块不超过 max_per 只，候选不足时放宽限制补足 pool_size。"""
    sector_counts: dict[str, int] = {}
    result = []
    skipped = []
    for s in candidates:
        sector = s.get("sector", "未知")
        if sector_counts.get(sector, 0) >= max_per:
            skipped.append(s)
            continue
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        result.append(s)
        if len(result) >= pool_size:
            break
    # 候选不足时，从被跳过的股票中补足（放宽板块集中度）
    if len(result) < pool_size and skipped:
        for s in skipped:
            result.append(s)
            if len(result) >= pool_size:
                break
    logger.info("板块集中度管控: %d 只 → %d 只 (max %d/板块, 目标%d)",
                 len(candidates), len(result), max_per, pool_size)
    return result


# ── 告警推送 ──────────────────────────────────────────────

_ALERT_COOLDOWN: dict[str, float] = {}   # v4.0: 告警去重冷却
_ALERT_INTERVAL = 1800                    # 同类型告警最小间隔（秒）


def _send_alert(title: str, content: str):
    """通过 Server酱 推送告警。失败时仅打日志。"""
    # v4.0: 去重 — 同标题告警 30 分钟内不重复发送
    now = time.time()
    last = _ALERT_COOLDOWN.get(title, 0)
    if now - last < _ALERT_INTERVAL:
        return
    _ALERT_COOLDOWN[title] = now

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
    """检查并推送关键告警（已内置 30 分钟冷却）。"""
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
    # Step 1: 获取热板块 — 双维度（资金流 + 价格动量），打破单一指标回声室
    hot_sectors = []
    if collector:
        all_data = collector.get_all_sectors_data()
        if all_data:
            concept = [s for s in all_data if s["type"] == "concept"]
            industry = [s for s in all_data if s["type"] == "industry"]

            # 维度 A：资金流 Top 5
            by_flow_c = sorted(concept, key=lambda x: x["net_main"], reverse=True)[:HOT_SECTOR_COUNT]
            by_flow_i = sorted(industry, key=lambda x: x["net_main"], reverse=True)[:HOT_SECTOR_COUNT]
            # 维度 B：价格动量 Top 3
            by_pct_c = sorted(concept, key=lambda x: x.get("pct_chg", 0), reverse=True)[:HOT_SECTOR_COUNT_PCT]
            by_pct_i = sorted(industry, key=lambda x: x.get("pct_chg", 0), reverse=True)[:HOT_SECTOR_COUNT_PCT]

            # 合并去重
            seen_names = set()
            for s in by_flow_c + by_flow_i + by_pct_c + by_pct_i:
                name = s.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    hot_sectors.append(s)
            logger.info("从 collector 双维度选板块: 资金流%d+%d, 动量%d+%d → 去重%d",
                        len(by_flow_c), len(by_flow_i),
                        len(by_pct_c), len(by_pct_i), len(hot_sectors))
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

    # Step 2: 下钻成分股（并行拉取，max_workers=5 防打满连接池）
    def _fetch_one_sector(sector: dict) -> list:
        """拉取单个板块成分股并充实因子，返回带 sector 标记的 stock 列表。"""
        code = sector.get("code", "")
        if not code:
            return []
        stocks = []
        for attempt in range(3):
            stocks = _fetch_sector_stocks(code, top_n=STOCKS_PER_SECTOR)
            if stocks:
                break
            time.sleep(0.5)  # 重试前等待
        if not stocks:
            logger.warning("板块 %s(%s) 成分股3次重试均失败", sector["name"], code)
            return []
        # 充实因子：短动量 + 突破距离
        for s in stocks:
            daily = _DAILY_SCORES.get(s.get("code", ""), {})
            factor_sc = daily.get("factor_score", 0) if daily else 0
            s["short_momentum"] = round((factor_sc - 5) * 2, 2)
            dev = (s.get("pct_chg") or 0) - (sector.get("pct_chg") or 0)
            s["breakout_dist"] = round(dev * 5, 2)
            s["sector"] = sector["name"]
            s["sector_type"] = sector.get("type", "concept")
            s["sector_pct"] = sector.get("pct_chg", 0)
            s["price_deviation"] = round(
                (s.get("pct_chg") or 0) - (sector.get("pct_chg") or 0), 2)
        return stocks

    seen_sectors = set()
    deduped_sectors = []
    for sector in hot_sectors:
        if sector["name"] not in seen_sectors and sector.get("code"):
            seen_sectors.add(sector["name"])
            deduped_sectors.append(sector)

    all_stocks = []
    _t0 = time.time()
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_one_sector, s): s for s in deduped_sectors}
        for fut in as_completed(futures):
            all_stocks.extend(fut.result())
    logger.info("A池成分股并行拉取: %d板块 → %d只, 耗时 %.1fs",
                len(deduped_sectors), len(all_stocks), time.time() - _t0)

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

    # Step 4: 创业板/科创板/北交所 + 流动性过滤 + 涨停板过滤
    unique = [s for s in unique if not s["code"].startswith(("688", "8", "9"))]
    unique, _liq_removed = _apply_liquidity_filter(unique, pool_type="A")
    unique, _limit_up_removed = _apply_limit_up_filter(unique)

    # ── A 池：当日热力板块成分股 ──────────────────────────
    a_stocks = unique
    for s in a_stocks:
        s["pool"] = "A"  # 标记来源池

    # ── B 池：周期回调板块成分股 ──────────────────────────
    b_stocks_raw = []
    b_pullback_sectors = []
    _b_liq_removed = []
    _b_limit_up_removed = []
    try:
        reviewer = SectorReviewer()
        pullback_sectors = reviewer.get_pullback_sectors(
            collector=collector, top_n=PULLBACK_MAX_SECTORS)
        if pullback_sectors:
            logger.info("B 池回调板块: %d 个", len(pullback_sectors))
            # 去重 + 过滤无 code 板块
            b_deduped = []
            b_seen_names = set()
            for ps in pullback_sectors:
                if ps["name"] not in b_seen_names and ps.get("code"):
                    b_seen_names.add(ps["name"])
                    b_deduped.append(ps)

            def _fetch_one_b_sector(ps: dict) -> list:
                code = ps.get("code", "")
                stocks = []
                for attempt in range(3):
                    stocks = _fetch_sector_stocks(code, top_n=STOCKS_PER_SECTOR)
                    if stocks:
                        break
                    time.sleep(0.5)
                for s in stocks:
                    s["sector"] = ps["name"]
                    s["sector_type"] = ps.get("type", "concept")
                    s["sector_pct"] = ps.get("today_return", 0)
                    s["price_deviation"] = round(
                        (s.get("pct_chg") or 0) - (ps.get("today_return") or 0), 2)
                    s["pool"] = "B"
                return stocks

            _t0b = time.time()
            with ThreadPoolExecutor(max_workers=5) as ex:
                b_futures = {ex.submit(_fetch_one_b_sector, ps): ps for ps in b_deduped}
                for fut in as_completed(b_futures):
                    b_stocks_raw.extend(fut.result())
            logger.info("B池成分股并行拉取: %d板块 → %d只, 耗时 %.1fs",
                        len(b_deduped), len(b_stocks_raw), time.time() - _t0b)

            # B 池去重 + 过滤
            b_seen = {}
            for s in b_stocks_raw:
                code = s["code"]
                if code not in b_seen:
                    b_seen[code] = s
                else:
                    if (s.get("net_main_inflow") or 0) > (b_seen[code].get("net_main_inflow") or 0):
                        b_seen[code] = s
            b_unique = list(b_seen.values())
            b_unique = [s for s in b_unique if not s["code"].startswith(("688", "8", "9"))]
            b_unique, _b_liq_removed = _apply_liquidity_filter(b_unique, pool_type="B")
            b_unique, _b_limit_up_removed = _apply_limit_up_filter(b_unique)
            b_pullback_sectors = pullback_sectors
        else:
            b_unique = []
    except Exception as e:
        logger.warning("B 池构建失败: %s", e)
        b_unique = []
        b_pullback_sectors = []

    # Step 5: 大盘广度 → 风险等级
    breadth = _get_market_breadth()
    if breadth < EXTREME_BEAR_THRESHOLD:
        risk_level = "extreme"
        logger.info("⚠️ 极端熊市 (广度 %.0f%%) → A池禁用，全部转B池", breadth * 100)
    elif breadth < BEAR_MARKET_THRESHOLD:
        risk_level = "high"
        logger.info("普跌环境 (广度 %.0f%%)", breadth * 100)
    elif breadth < 0.30:
        risk_level = "medium"
    else:
        risk_level = "low"

    # v4.0: 识别熔断板块
    meltdown_sectors = _get_meltdown_sectors(hot_sectors)

    # Step 6: 日评分先合并，再评分排序（日评分参与盘中排名 25%）
    a_stocks = _merge_daily_scores(a_stocks, _DAILY_SCORES)
    a_ranked = _score_and_rank(a_stocks, pool_type="A")
    # v4.0: 急跌过滤 + 板块熔断惩罚
    a_ranked, _a_crash_removed = _apply_flash_crash_filter(a_ranked)
    a_ranked, _a_fraud_removed = _apply_fraud_filter(a_ranked)

    b_unique = _merge_daily_scores(b_unique, _DAILY_SCORES)
    b_ranked = _score_and_rank(b_unique, pool_type="B")
    b_ranked, _b_crash_removed = _apply_flash_crash_filter(b_ranked)
    b_ranked, _b_fraud_removed = _apply_fraud_filter(b_ranked)

    # v4.0: 板块熔断惩罚（在评分之后、集中度管控之前）
    _apply_sector_meltdown_penalty(a_ranked, meltdown_sectors)
    _apply_sector_meltdown_penalty(b_ranked, meltdown_sectors)

    # Step 7: 板块集中度管控（根据情绪面动态分配 A/B 池名额）
    try:
        from signals import get_signal_store, get_pool_allocation
        store = get_signal_store()
        alloc = get_pool_allocation(store.get_all())
        a_size = min(50, max(5, alloc["pool_a"]))
        b_size = min(50, max(5, alloc["pool_b"]))
    except Exception:
        a_size, b_size = 25, 25  # 降级：固定分配

    # v4.0: 极端熊市 → A池强制归零，全部转B池
    if risk_level == "extreme":
        a_size = 0
        b_size = min(50, b_size + a_size if a_size else 50)
        logger.info("极端熊市熔断: A池禁用, B池扩容至%d", b_size)

    a_candidates = _apply_sector_concentration(
        a_ranked, max_per=MAX_PER_SECTOR, pool_size=a_size
    )
    b_candidates = _apply_sector_concentration(
        b_ranked, max_per=MAX_PER_SECTOR_B, pool_size=b_size
    )

    # Step 8: 截断 + 聚合分修正（情绪/消息介入）
    a_candidates = a_candidates[:a_size]
    b_candidates = b_candidates[:b_size]

    # 聚合分：在候选截断后计算，只影响最终排序
    try:
        signals = store.get_all()
        weights = store.get_effective_weights()
        for c in a_candidates + b_candidates:
            c["base_score"] = c.get("score", 0)
            c["aggregated_score"] = compute_final_score(c, signals, weights)
        # 按聚合分重排
        a_candidates.sort(key=lambda x: x.get("aggregated_score", 0), reverse=True)
        b_candidates.sort(key=lambda x: x.get("aggregated_score", 0), reverse=True)
    except Exception:
        pass  # 聚合失败时保持原始排序
    # 简单合并（不去重），供向后兼容的 candidates 字段使用
    # 优先按聚合分排序，无聚合分时回退到原始分
    candidates = a_candidates + b_candidates
    candidates.sort(key=lambda x: x.get("aggregated_score", x.get("score", 0)), reverse=True)

    logger.info("候选池: A池%d(目标%d) + B池%d(目标%d) → %d 只",
                 len(a_candidates), a_size, len(b_candidates), b_size, len(candidates))

    # Step 9: 附加信号操作指引 + risk_level + ATR动态止盈止损
    for c in candidates:
        signal = c.get("signal", "盘中观察")
        guide = SIGNAL_GUIDE.get(signal, SIGNAL_GUIDE["盘中观察"])
        c["holding_days"] = guide["holding"]
        c["take_profit"] = guide["take_profit"]
        c["stop_loss"] = guide["stop_loss"]
        # v4.0: ATR 动态止盈止损（双轨展示）
        c.update(_estimate_atr_stops(c))

    # Step 10: 告警检查
    _check_alerts(
        candidates=candidates,
        hot_sectors=hot_sectors,
        daily_scores_ok=bool(_DAILY_SCORES),
        api_failures=0,
    )
    # 额外：池大小异常告警
    if len(a_candidates) < 10:
        _send_alert("⚠️ A池候选不足", f"A池仅 {len(a_candidates)} 只 (目标 25)")
    if len(b_candidates) < 10:
        _send_alert("⚠️ B池候选不足", f"B池仅 {len(b_candidates)} 只 (目标 25)")

    # 合并热板块名（A池 + B池）
    all_sector_names = ([s["name"] for s in hot_sectors] +
                        [s["name"] for s in b_pullback_sectors])

    # v4.0: 尾盘确认标记 — 14:45 后为最终确认池
    now = datetime.now()
    final_call = (now.hour == 14 and now.minute >= 45) or (now.hour == 15 and now.minute == 0)

    return {"time": _now_time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(candidates),
        "candidates": candidates,
        "pool_a": a_candidates,
        "pool_b": b_candidates,
        "hot_sectors": all_sector_names,
        "pullback_sectors": [s["name"] for s in b_pullback_sectors],
        "risk_level": risk_level,
        "market_breadth": round(breadth, 2),
        "final_call": final_call,
        "filter_stats": {
            "liquidity_removed": len(_liq_removed) + len(_b_liq_removed),
            "limit_up_removed": len(_limit_up_removed) + len(_b_limit_up_removed),
            "fraud_removed": len(_a_fraud_removed) + len(_b_fraud_removed),
            "pool_size": 50,
            "pool_a": len(a_candidates),
            "pool_b": len(b_candidates),
        },
        "mode": "live"}


def _select_stocks_mock():
    """模拟选股（测试用）— A 池 + B 池双轨。"""
    # ── A 池 mock ──────────────────────────────────────────
    # 使用全部板块（而非仅前 5 个），确保经过 300/688 过滤后仍有足够候选填满 25 只
    sectors = list(_MOCK_STOCKS_BY_SECTOR.keys())[:10]
    a_result = []
    for sector in sectors:
        stocks_list = _MOCK_STOCKS_BY_SECTOR.get(sector, [])
        sector_pct = round(random.uniform(0.5, 4.0), 2)
        for code, name in stocks_list:
            pct_chg = round(random.uniform(-2, 6), 2)
            price = round(random.uniform(10, 200), 2)
            intra_pos = round(random.uniform(0.1, 0.95), 2)
            open_ret = round(random.uniform(-2, 4), 2)
            a_result.append({"code": code, "name": name, "sector": sector,
                "sector_type": "concept", "pool": "A",
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
                "short_momentum": round(random.uniform(-8, 15), 2),
                "breakout_dist": round(random.uniform(-10, 5), 2),
                "sector_pct": sector_pct,
                "price_deviation": round(pct_chg - sector_pct, 2)})

    a_result = _merge_daily_scores(a_result, _DAILY_SCORES)
    a_ranked = _score_and_rank(a_result, pool_type="A")
    a_ranked = [s for s in a_ranked if not s["code"].startswith(("688", "8", "9"))]
    # 动态池大小（mock 模式下降级为固定值）
    try:
        from signals import get_signal_store, get_pool_allocation
        alloc = get_pool_allocation(get_signal_store().get_all())
        a_mock_size = min(50, max(5, alloc["pool_a"]))
        b_mock_size = min(50, max(5, alloc["pool_b"]))
    except Exception:
        a_mock_size, b_mock_size = 25, 25
    a_candidates = _apply_sector_concentration(a_ranked, max_per=MAX_PER_SECTOR, pool_size=a_mock_size)[:a_mock_size]

    # ── B 池 mock（模拟回调板块个股）─────────────────────────
    pullback_sector_names = ["军工", "半导体", "新能源车", "光伏", "消费电子", "创新药"]
    b_result = []
    for sector in pullback_sector_names:
        stocks_list = _MOCK_STOCKS_BY_SECTOR.get(sector, _MOCK_INDUSTRY_SECTORS.get(sector, []))
        sector_pct = round(random.uniform(-3.0, -0.5), 2)  # 回调板块微跌
        for code, name in stocks_list:
            pct_chg = round(random.uniform(-4, 1), 2)
            price = round(random.uniform(10, 200), 2)
            intra_pos = round(random.uniform(0.1, 0.5), 2)  # 低位
            open_ret = round(random.uniform(-3, 0.5), 2)
            b_result.append({"code": code, "name": name, "sector": sector,
                "sector_type": "concept", "pool": "B",
                "price": price,
                "pct_chg": pct_chg,
                "net_main_inflow": round(random.uniform(-1, 8), 2),
                "net_main_ratio": round(random.uniform(-3, 15), 2),
                "volume_ratio": round(random.uniform(0.5, 2.0), 2),
                "turnover_rate": round(random.uniform(0.5, 8.0), 1),
                "amp_ratio": round(random.uniform(1, 5), 1),
                "market_cap": round(random.uniform(30, 500), 1),
                "intraday_position": intra_pos,
                "open_return": open_ret,
                "short_momentum": round(random.uniform(-8, 10), 2),
                "breakout_dist": round(random.uniform(-12, 3), 2),
                "sector_pct": sector_pct,
                "price_deviation": round(pct_chg - sector_pct, 2)})

    b_result = _merge_daily_scores(b_result, _DAILY_SCORES)
    b_ranked = _score_and_rank(b_result, pool_type="B")
    b_ranked = [s for s in b_ranked if not s["code"].startswith(("688", "8", "9"))]
    b_candidates = _apply_sector_concentration(b_ranked, max_per=MAX_PER_SECTOR_B, pool_size=b_mock_size)[:b_mock_size]

    # ── 各自截断合并 ────────────────────────────────────────
    # (已在上面用动态大小截断)
    ranked = a_candidates + b_candidates
    ranked.sort(key=lambda x: x.get("aggregated_score", x.get("score", 0)), reverse=True)

    if not _DAILY_SCORES:
        signal_bank = ["放量突破", "MA5金叉MA10", "均线多头排列", "MACD金叉",
                       "KDJ超卖金叉", "RSI上穿50", "连续3日放量", "涨停回踩10日线",
                       "平台突破", "连板梯队", "情绪周期"]
        for c in ranked:
            n_sig = random.randint(1, 5)
            c["daily_combined_score"] = round(random.uniform(2, 10), 1)
            c["daily_signal_count"] = n_sig
            c["daily_signal_names"] = " | ".join(
                random.sample(signal_bank, min(n_sig, len(signal_bank))))

    # 附加信号指引 + ATR动态止盈止损
    for c in ranked:
        guide = SIGNAL_GUIDE.get(c.get("signal", "盘中观察"), SIGNAL_GUIDE["盘中观察"])
        c["holding_days"] = guide["holding"]
        c["take_profit"] = guide["take_profit"]
        c["stop_loss"] = guide["stop_loss"]
        c.update(_estimate_atr_stops(c))

    hot_names = sectors + list(_MOCK_INDUSTRY_SECTORS.keys())[:2]
    candidates = ranked
    now = datetime.now()
    final_call = (now.hour == 14 and now.minute >= 45) or (now.hour == 15 and now.minute == 0)
    return {"time": _now_time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(candidates),
        "candidates": candidates,
        "pool_a": a_candidates,
        "pool_b": b_candidates,
        "hot_sectors": hot_names,
        "pullback_sectors": pullback_sector_names,
        "risk_level": "low",
        "market_breadth": 0.55,
        "final_call": final_call,
        "filter_stats": {"liquidity_removed": 0, "limit_up_removed": 0,
                          "fraud_removed": 0, "pool_size": 50,
                          "pool_a": len(a_candidates), "pool_b": len(b_candidates)},
        "mode": "live" if _DAILY_SCORES else "standalone"}


# ── 全局状态 ────────────────────────────────────────────────

_DAILY_SCORES = {}
_ALL_DAILY_SCORES_SORTED = []     # 全部日评分的排序列表（用于百分位归一化）
_LAST_REAL_RESULT: dict = {}      # 持久化最后一份真实选股结果
_LAST_REAL_RESULT_FILE = Path(__file__).parent / "data" / "last_picks_result.json"
_CACHE = {}
_CACHE_TTL = 0.0
_CACHE_LIFETIME = 60.0


def _save_last_result(result: dict):
    """持久化最后一份真实选股结果到 JSON 文件。"""
    try:
        DATA_DIR_INNER = Path(__file__).parent / "data"
        DATA_DIR_INNER.mkdir(parents=True, exist_ok=True)
        # 只保存关键字段，去掉不可序列化的内容
        saved = {
            "time": result.get("time", ""),
            "date": result.get("date", ""),
            "total": result.get("total", 0),
            "candidates": result.get("candidates", []),
            "pool_a": result.get("pool_a", []),
            "pool_b": result.get("pool_b", []),
            "hot_sectors": result.get("hot_sectors", []),
            "pullback_sectors": result.get("pullback_sectors", []),
            "risk_level": result.get("risk_level", "low"),
            "market_breadth": result.get("market_breadth", 0.5),
            "filter_stats": result.get("filter_stats", {}),
            "mode": "cached",
            "cached_at": datetime.now().strftime("%Y-%m-%d") + " " + _now_time(),
        }
        with open(_LAST_REAL_RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning("持久化选股结果失败: %s", e)


def _load_last_result() -> dict:
    """加载持久化的最后一份真实选股结果。"""
    try:
        if _LAST_REAL_RESULT_FILE.exists():
            with open(_LAST_REAL_RESULT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["mode"] = "cached"
            try:
                from data_fetcher import _is_trading_time
                data["is_trading"] = _is_trading_time()
                data["data_date"] = data.get("date", data.get("time", "")[:10])
            except Exception:
                data["is_trading"] = False
            return data
    except Exception:
        pass
    return {}


def select_stocks(collector=None) -> dict:
    """盘中选股入口。

    - 交易时段：实时选股，失败时用持久化缓存
    - 非交易时段：返回持久化的最后一份真实结果
    - 永不使用模拟数据

    Args:
        collector: SectorFlowCollector 实例（可选，用于数据复用）
    """
    global _CACHE, _CACHE_TTL, _DAILY_SCORES, _LAST_REAL_RESULT

    # 加载日评分（首次调用）
    if not _DAILY_SCORES:
        _DAILY_SCORES.update(load_daily_scores())
        if _DAILY_SCORES:
            logger.info("加载日评分缓存: %d 只股票", len(_DAILY_SCORES))
        else:
            logger.info("日评分缓存缺失，自动生成兜底评分...")
            _DAILY_SCORES.update(_generate_fallback_scores())
        # 构建全量排序列表供百分位归一化
        if _DAILY_SCORES:
            _ALL_DAILY_SCORES_SORTED[:] = sorted(
                v["combined_score"] for v in _DAILY_SCORES.values()
            )

    # 缓存控制（仅交易时段生效）
    now = time.time()
    if _CACHE and (now - _CACHE_TTL) < _CACHE_LIFETIME:
        return _CACHE

    try:
        result = _select_stocks_real(collector=collector)
        # 成功获取实时数据，持久化
        _LAST_REAL_RESULT = result
        try:
            from data_fetcher import _is_trading_time
            result["is_trading"] = _is_trading_time()
            result["data_date"] = result.get("date", "")
        except Exception:
            result["is_trading"] = True
        _save_last_result(result)
    except Exception as e:
        logger.warning("实时选股失败: %s，使用持久化缓存", e)
        result = _LAST_REAL_RESULT or _load_last_result()
        if result:
            result["mode"] = "cached"
            result["cached_at"] = _LAST_REAL_RESULT.get(
                "cached_at", result.get("time", ""))
        else:
            # 完全没有数据时返回空
            result = {
                "time": _now_time(),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "total": 0, "candidates": [],
                "pool_a": [], "pool_b": [],
                "hot_sectors": [], "pullback_sectors": [],
                "risk_level": "low", "market_breadth": 0.5,
                "filter_stats": {}, "mode": "empty",
                "is_trading": False,
            }

    _CACHE = result
    _CACHE_TTL = now
    return result


# ── 后台预热 ─────────────────────────────────────────────

_WARMUP_INTERVAL = 50.0  # 略小于 _CACHE_LIFETIME(60s)，确保缓存始终热
_warmup_thread: Optional[threading.Thread] = None
_warmup_stop = threading.Event()


def _warmup_loop():
    """后台预热线程：交易时段定期刷新选股缓存。"""
    global _warmup_stop
    logger.info("选股预热线程已启动 (间隔 %.0fs)", _WARMUP_INTERVAL)
    while not _warmup_stop.is_set():
        try:
            _warmup_stop.wait(_WARMUP_INTERVAL)
            if _warmup_stop.is_set():
                break
            # 检查是否交易时段
            try:
                from data_fetcher import _is_trading_time
                if not _is_trading_time():
                    continue
            except Exception:
                pass
            # 执行预热
            _t0 = time.time()
            select_stocks()
            logger.info("选股预热刷新完成, 耗时 %.1fs", time.time() - _t0)
        except Exception as e:
            logger.warning("选股预热失败: %s", e)


def start_stock_warmup():
    """启动后台选股预热线程（应用启动时调用）。"""
    global _warmup_thread, _warmup_stop
    if _warmup_thread and _warmup_thread.is_alive():
        return
    _warmup_stop.clear()
    _warmup_thread = threading.Thread(target=_warmup_loop, daemon=True, name="stock-warmup")
    _warmup_thread.start()


def stop_stock_warmup():
    """停止后台选股预热线程。"""
    global _warmup_stop
    _warmup_stop.set()


# ── CLI 测试 ────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = select_stocks(use_mock=True)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
    print(f"\n共 {data['total']} 只候选 · A池{len(data.get('pool_a', []))} B池{len(data.get('pool_b', []))} · 模式={data['mode']}")
    for c in data['candidates'][:10]:
        print(f"  {c['name']:6s} {c['sector']:6s} "
              f"涨{c['pct_chg']:>+5.1f}% "
              f"主力{c['net_main_inflow']:>5.1f}亿 "
              f"净占比{c['net_main_ratio']:>5.1f}% "
              f"量比{c['volume_ratio']:>4.1f} "
              f"日内位{c['intraday_position']:.2f} "
              f"开涨幅{c['open_return']:>+5.1f}% "
              f"评分{c['score']:.3f} {c['signal']}")
