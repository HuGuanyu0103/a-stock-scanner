"""
情绪面信号识别 — 涨停梯队分析、连板高度、情绪周期

对标 WalkerLau/stock 的涨停梯队和连板情绪周期分析，
直接从 K 线和行情数据计算短线情绪类信号。
每个信号函数返回 SignalList（与 patterns.py 格式一致）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from layer2_scan.patterns import SignalList, SignalDict

logger = logging.getLogger(__name__)


# ── 辅助: 判断是否涨停 ─────────────────────────────────────────

def _is_limit_up(row, pct_col: str = "change_pct") -> bool:
    """判断单根 K 线是否涨停（涨幅 >= 9.8%）"""
    return float(row.get(pct_col, 0)) >= 9.8


# ── 信号: 连板梯队识别 ────────────────────────────────────────

def check_board_tier(df: pd.DataFrame) -> SignalList:
    """
    连板梯队信号 — 识别个股的连续涨停梯队位置

    信号强度:
    - 首板(1板): level 3
    - 2-3板: level 4
    - 4-5板: level 5
    - 6板+: level 5 (龙头信号)
    """
    signals = []
    if df.empty or len(df) < 3:
        return signals

    pct_col = "change_pct" if "change_pct" in df.columns else "pct_chg"
    recent = df.tail(10).sort_values("trade_date", ascending=False)
    board_count = 0
    for _, row in recent.iterrows():
        if float(row.get(pct_col, 0)) >= 9.8:
            board_count += 1
        else:
            break

    if board_count == 0:
        return signals

    if board_count == 1:
        level = 3
        tier_name = "首板"
    elif board_count <= 3:
        level = 4
        tier_name = f"{board_count}连板"
    elif board_count <= 5:
        level = 5
        tier_name = f"{board_count}连板(高位)"
    else:
        level = 5
        tier_name = f"{board_count}连板(龙头)"

    signals.append({
        "code": "board_tier",
        "name": tier_name,
        "level": level,
        "desc": f"连续{board_count}个涨停板",
    })
    return signals


# ── 信号: 炸板回封 ────────────────────────────────────────────

def check_reopen_after_break(df: pd.DataFrame) -> SignalList:
    """
    炸板回封信号 — 当日高开低走后又拉回（长下影线+小幅上涨）

    识别特征:
    - 日 K 线有长下影线 (下影线/实体 > 1.5)
    - 收盘微涨 (change_pct 在 0-3% 之间)
    - 成交量放大
    """
    signals = []
    if df.empty or len(df) < 2:
        return signals

    last = df.iloc[-1]
    pct_col = "change_pct" if "change_pct" in df.columns else "pct_chg"
    pct = float(last.get(pct_col, 0))
    o = float(last.get("open", 0))
    c = float(last.get("close", 0))
    h = float(last.get("high", 0))
    l = float(last.get("low", 0))

    if o == 0 or c == 0:
        return signals

    if not (0 < pct < 4):
        return signals

    body = abs(c - o)
    lower_shadow = min(o, c) - l
    if body > 0 and lower_shadow / body > 1.0 and lower_shadow > 0.01:
        vol = last.get("volume", 0)
        avg_vol = df["volume"].tail(20).mean() if len(df) >= 20 else df["volume"].mean()
        signals.append({
            "code": "reopen_break",
            "name": "炸板回封",
            "level": 3 if vol > avg_vol * 1.2 else 2,
            "desc": f"下影线{lower_shadow:.2f}，收{c:.2f}，涨幅{pct:.1f}%",
        })
    return signals


# ── 信号: 弱转强 ───────────────────────────────────────────────

def check_weak_to_strong(df: pd.DataFrame) -> SignalList:
    """
    弱转强信号 — 前日收阴/大跌，今日放量反包

    识别特征:
    - 前日收跌 (change_pct < -2%)
    - 今日收涨且涨幅 > 前日跌幅的 50%
    - 今日成交量 > 昨日成交量
    """
    signals = []
    if df.empty or len(df) < 3:
        return signals

    pct_col = "change_pct" if "change_pct" in df.columns else "pct_chg"
    prev = df.iloc[-2]
    last = df.iloc[-1]

    prev_pct = float(prev.get(pct_col, 0))
    cur_pct = float(last.get(pct_col, 0))

    if prev_pct < -2 and cur_pct > 0 and cur_pct > abs(prev_pct) * 0.5:
        prev_vol = float(prev.get("volume", 0))
        cur_vol = float(last.get("volume", 0))
        vol_confirm = cur_vol > prev_vol * 1.1
        signals.append({
            "code": "weak_to_strong",
            "name": "弱转强反包",
            "level": 4 if vol_confirm else 3,
            "desc": f"前日{prev_pct:.1f}%→今日{cur_pct:.1f}%，"
                    f"{'放量' if vol_confirm else '平量'}反包",
        })
    return signals


# ── 信号: 情绪周期位置（基于个股与大盘对比）──────────────────

def check_sentiment_cycle(df: pd.DataFrame, benchmark_pct: float = 0) -> SignalList:
    """
    情绪周期位置信号 — 个股相对市场的强弱判断

    benchmark_pct: 大盘指数当日涨跌幅（可选）
    """
    signals = []
    if df.empty or len(df) < 20:
        return signals

    pct_col = "change_pct" if "change_pct" in df.columns else "pct_chg"
    last_pct = float(df.iloc[-1].get(pct_col, 0))

    # 计算个股相对强度（近5日涨幅 vs 近20日波动）
    close = df["close"]
    ret_5d = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100 if len(df) >= 6 else 0
    ret_20d = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100 if len(df) >= 21 else 0

    # 近10日波动率
    volatility = close.tail(10).pct_change().std() * 100 if len(df) >= 11 else 0

    if last_pct > 5 and ret_5d > 10 and volatility > 3:
        signals.append({
            "code": "sentiment_cycle",
            "name": "加速冲顶",
            "level": 1,
            "desc": f"近5日涨{ret_5d:.1f}%，波动{volatility:.1f}%",
        })
    elif last_pct > 3 and ret_20d > 5 and ret_5d > ret_20d:
        signals.append({
            "code": "sentiment_cycle",
            "name": "主升浪",
            "level": 4,
            "desc": f"近20日涨{ret_20d:.1f}%，近5日涨{ret_5d:.1f}%",
        })
    elif last_pct > 0 and ret_20d < -5 and ret_5d > 0:
        signals.append({
            "code": "sentiment_cycle",
            "name": "超跌反弹",
            "level": 3,
            "desc": f"近20日跌{abs(ret_20d):.1f}%，今日反弹{last_pct:.1f}%",
        })

    return signals


# ── 注册所有情绪信号 ──────────────────────────────────────────

SENTIMENT_SIGNAL_CHECKS = [
    check_board_tier,
    check_reopen_after_break,
    check_weak_to_strong,
    check_sentiment_cycle,
]


class SentimentSignals:
    """情绪信号扫描器"""

    @staticmethod
    def scan(df: pd.DataFrame, benchmark_pct: float = 0) -> SignalList:
        all_signals = []
        for check_fn in SENTIMENT_SIGNAL_CHECKS:
            try:
                sigs = check_fn(df, benchmark_pct) if check_fn.__name__ == "check_sentiment_cycle" else check_fn(df)
                all_signals.extend(sigs)
            except Exception as e:
                logger.debug("情绪信号 %s 失败: %s", check_fn.__name__, e)
        return all_signals

    @staticmethod
    def total_score(signals: SignalList) -> float:
        return sum(s["level"] for s in signals)

    @staticmethod
    def signal_names(signals: SignalList) -> list[str]:
        return [s["name"] for s in signals]
