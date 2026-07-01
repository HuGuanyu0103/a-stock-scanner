"""
技术形态识别 — 为短线 2-14 天周期设计的 A 股技术形态/模式识别

每个形态返回形如 {"code": "信号名", "level": 1-5, "desc": "说明"} 的结构。
level 越高信号越强。
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 辅助计算函数 ─────────────────────────────────────────────────

def _ma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def _macd(close: pd.Series):
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    macd_hist = 2 * (dif - dea)
    return dif, dea, macd_hist

def _kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9):
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = (close - low_n) / (high_n - low_n + 1e-9) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def _rsi(close: pd.Series, n: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))

SignalDict = dict[str, object]
SignalList = list[SignalDict]

# ── 形态识别 ─────────────────────────────────────────────────────

def check_volume_breakout(df: pd.DataFrame) -> SignalList:
    """放量突破 — 涨幅>3% 且 成交量>5日均量×1.5"""
    signals = []
    if df.empty or len(df) < 6:
        return signals
    last = df.iloc[-1]
    prev = df.iloc[:-1]
    vol_avg5 = prev["volume"].tail(5).mean()
    pct = last.get("change_pct", last.get("pct_chg", 0))
    if pct > 3 and last["volume"] > vol_avg5 * 1.5:
        level = 4 if pct > 5 else 3
        signals.append({
            "code": "volume_breakout",
            "name": "放量突破",
            "level": level,
            "desc": f"涨幅 {pct:.1f}%，量比 {last['volume']/vol_avg5:.1f}x",
        })
    return signals

def check_ma_golden_cross(df: pd.DataFrame) -> SignalList:
    """均线金叉 — MA5 上穿 MA10"""
    signals = []
    if df.empty or len(df) < 11:
        return signals
    close = df["close"]
    ma5 = _ma(close, 5)
    ma10 = _ma(close, 10)
    if ma5.iloc[-2] <= ma10.iloc[-2] and ma5.iloc[-1] > ma10.iloc[-1]:
        cross_val = abs(ma5.iloc[-1] - ma10.iloc[-1]) / ma10.iloc[-1] * 100
        signals.append({
            "code": "ma_golden_cross",
            "name": "MA5金叉MA10",
            "level": 3,
            "desc": f"交叉幅度 {cross_val:.2f}%",
        })
    return signals

def check_ma_bullish(df: pd.DataFrame) -> SignalList:
    """均线多头排列 — MA5 > MA10 > MA20"""
    signals = []
    if df.empty or len(df) < 21:
        return signals
    close = df["close"]
    ma5 = _ma(close, 5).iloc[-1]
    ma10 = _ma(close, 10).iloc[-1]
    ma20 = _ma(close, 20).iloc[-1]
    if ma5 > ma10 > ma20:
        spread = (ma5 - ma20) / ma20 * 100
        level = 4 if spread < 8 else 3
        signals.append({
            "code": "ma_bullish",
            "name": "均线多头排列",
            "level": level,
            "desc": f"MA5({ma5:.2f}) > MA10({ma10:.2f}) > MA20({ma20:.2f})",
        })
    return signals

def check_macd_golden_cross(df: pd.DataFrame) -> SignalList:
    """MACD 金叉 — DIF 上穿 DEA"""
    signals = []
    if df.empty or len(df) < 27:
        return signals
    dif, dea, hist = _macd(df["close"])
    if dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]:
        above_zero = dif.iloc[-1] > 0
        level = 4 if above_zero else 3
        signals.append({
            "code": "macd_golden_cross",
            "name": "MACD金叉",
            "level": level,
            "desc": f"DIF={dif.iloc[-1]:.2f}, DEA={dea.iloc[-1]:.2f}",
        })
    return signals

def check_kdj_golden_cross(df: pd.DataFrame) -> SignalList:
    """KDJ 金叉 — K 上穿 D，超卖区域信号更强"""
    signals = []
    if df.empty or len(df) < 10:
        return signals
    k, d, j = _kdj(df["high"], df["low"], df["close"])
    if k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1]:
        oversold = k.iloc[-1] < 30
        level = 4 if oversold else 2
        signals.append({
            "code": "kdj_golden_cross",
            "name": "KDJ超卖金叉" if oversold else "KDJ金叉",
            "level": level,
            "desc": f"K={k.iloc[-1]:.1f}, D={d.iloc[-1]:.1f}",
        })
    return signals

def check_rsi_mid_cross(df: pd.DataFrame, period: int = 14) -> SignalList:
    """RSI 上穿 50 — 短线由弱转强"""
    signals = []
    if df.empty or len(df) < period + 1:
        return signals
    rsi = _rsi(df["close"], period)
    if rsi.iloc[-2] <= 50 and rsi.iloc[-1] > 50:
        signals.append({
            "code": "rsi_mid_cross",
            "name": "RSI上穿50",
            "level": 3,
            "desc": f"RSI={rsi.iloc[-1]:.1f}",
        })
    return signals

def check_consecutive_volume(df: pd.DataFrame) -> SignalList:
    """连续放量 — 连续 3 天成交量递增"""
    signals = []
    if df.empty or len(df) < 4:
        return signals
    vol = df["volume"].tail(4)
    if vol.iloc[-3] < vol.iloc[-2] < vol.iloc[-1]:
        signals.append({
            "code": "consecutive_volume",
            "name": "连续3日放量",
            "level": 3,
            "desc": f"量: {vol.iloc[-3]:.0f}→{vol.iloc[-2]:.0f}→{vol.iloc[-1]:.0f}",
        })
    return signals

def check_limit_pullback(df: pd.DataFrame) -> SignalList:
    """涨停回调 — 7日内有大涨，当前回落靠近10日线"""
    signals = []
    if df.empty or len(df) < 11:
        return signals
    recent = df.tail(7)
    day_pct_col = "change_pct" if "change_pct" in df.columns else "pct_chg"
    day_pcts = df[day_pct_col].tail(7)
    has_big_day = any(abs(p) >= 7 for p in day_pcts)
    if has_big_day:
        ma10 = _ma(df["close"], 10).iloc[-1]
        cur_close = df["close"].iloc[-1]
        dist = abs(cur_close - ma10) / ma10 * 100
        if dist < 3:
            signals.append({
                "code": "limit_pullback",
                "name": "涨停回踩10日线",
                "level": 4,
                "desc": f"收{cur_close:.2f}，MA10={ma10:.2f}，偏离{dist:.1f}%",
            })
    return signals

def check_platform_breakout(df: pd.DataFrame, lookback: int = 20) -> SignalList:
    """平台突破 — 股价突破近 20 日最高点"""
    signals = []
    if df.empty or len(df) < lookback + 1:
        return signals
    window = df.tail(lookback + 1)
    high_20 = window["high"].iloc[:-1].max()
    cur_high = window["high"].iloc[-1]
    cur_close = window["close"].iloc[-1]
    if cur_high > high_20 and cur_close > high_20 * 0.98:
        vol = window["volume"].iloc[-1]
        vol_avg = window["volume"].iloc[:-1].mean()
        has_vol = vol > vol_avg * 1.3
        signals.append({
            "code": "platform_breakout",
            "name": "平台突破" if has_vol else "平台突破(量不足)",
            "level": 4 if has_vol else 2,
            "desc": f"突破{lookback}日高点{high_20:.2f}，收{cur_close:.2f}",
        })
    return signals

# ── 注册所有形态检查函数 ────────────────────────────────────────

PATTERN_CHECKS: list = [
    check_volume_breakout,
    check_ma_golden_cross,
    check_ma_bullish,
    check_macd_golden_cross,
    check_kdj_golden_cross,
    check_rsi_mid_cross,
    check_consecutive_volume,
    check_limit_pullback,
    check_platform_breakout,
]

class TechnicalPatterns:
    """技术形态扫描器"""

    @staticmethod
    def scan(df: pd.DataFrame) -> SignalList:
        all_signals = []
        for check_fn in PATTERN_CHECKS:
            try:
                sigs = check_fn(df)
                all_signals.extend(sigs)
            except Exception as e:
                logger.debug("形态检查 %s 失败: %s", check_fn.__name__, e)
        return all_signals

    @staticmethod
    def max_signal_level(signals: SignalList) -> int:
        return max((s["level"] for s in signals), default=0)

    @staticmethod
    def signal_names(signals: SignalList) -> list[str]:
        return [s["name"] for s in signals]

    @staticmethod
    def total_score(signals: SignalList) -> float:
        return sum(s["level"] for s in signals)
