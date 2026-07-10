"""
量化因子引擎 — 对标 Qlib 的因子表达和因子组合

用 pandas + scikit-learn 实现轻量级因子系统，无需安装 qlib。
每个因子计算出标准化分数 (z-score)，支持因子组合加权评分。

因子分为：
- 动量因子: 短期/中期/长期动量、反转
- 波动因子: 波动率、振幅、ATR
- 量价因子: 量价配合、换手率变化
- 形态因子: 均线斜率、MACD 状态
- 资金面因子: 资金流向占比

使用方法:
    factors = AlphaFactors(kline_df)
    result = factors.compute_all()
    score = factors.composite_score(weights={...})
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 类型别名
FactorDict = dict


class AlphaFactors:
    """个股因子计算引擎"""

    def __init__(self, kline: pd.DataFrame):
        """
        Parameters
        ----------
        kline : DataFrame 必须包含 columns: trade_date, open, close, high, low, volume
        """
        self.df = kline.copy().sort_values("trade_date").reset_index(drop=True)
        self._pct_col = "change_pct" if "change_pct" in self.df.columns else "pct_chg"
        self._factors: dict[str, float] = {}

    # ── 动量因子 ─────────────────────────────────────────────────

    def factor_mom_1d(self) -> float:
        """当日动量（涨跌幅）"""
        val = float(self.df[self._pct_col].iloc[-1]) if len(self.df) >= 1 else 0
        self._factors["mom_1d"] = val
        return val

    def factor_mom_5d(self) -> float:
        """5日动量"""
        if len(self.df) < 6:
            return 0
        val = (self.df["close"].iloc[-1] / self.df["close"].iloc[-6] - 1) * 100
        self._factors["mom_5d"] = val
        return val

    def factor_mom_10d(self) -> float:
        """10日动量"""
        if len(self.df) < 11:
            return 0
        val = (self.df["close"].iloc[-1] / self.df["close"].iloc[-11] - 1) * 100
        self._factors["mom_10d"] = val
        return val

    def factor_mom_20d(self) -> float:
        """20日动量"""
        if len(self.df) < 21:
            return 0
        val = (self.df["close"].iloc[-1] / self.df["close"].iloc[-21] - 1) * 100
        self._factors["mom_20d"] = val
        return val

    def factor_reversal_3d(self) -> float:
        """3日反转因子 — 近3日涨得多则看空，反之看多"""
        if len(self.df) < 4:
            return 0
        ret = (self.df["close"].iloc[-1] / self.df["close"].iloc[-4] - 1) * 100
        val = -ret  # 反转: 涨多了减分
        self._factors["reversal_3d"] = val
        return val

    def factor_mom_ratio(self) -> float:
        """动量加速度 — (5日动量 - 20日动量)，上升加速为正"""
        mom5 = self.factor_mom_5d()
        mom20 = self.factor_mom_20d()
        val = mom5 - mom20
        self._factors["mom_ratio"] = val
        return val

    # ── 波动因子 ─────────────────────────────────────────────────

    def factor_volatility_5d(self) -> float:
        """5日波动率"""
        if len(self.df) < 6:
            return 0
        val = self.df["close"].tail(6).pct_change().std() * 100
        self._factors["volatility_5d"] = val
        return val

    def factor_volatility_20d(self) -> float:
        """20日波动率"""
        if len(self.df) < 21:
            return 0
        val = self.df["close"].tail(21).pct_change().std() * 100
        self._factors["volatility_20d"] = val
        return val

    def factor_atr(self) -> float:
        """平均真实波幅 ATR(14) / 收盘价 %"""
        if len(self.df) < 15:
            return 0
        high, low, close = self.df["high"], self.df["low"], self.df["close"].shift(1)
        tr = pd.concat([
            self.df["high"] - self.df["low"],
            (self.df["high"] - close).abs(),
            (self.df["low"] - close).abs(),
        ], axis=1).max(axis=1)
        val = float(tr.tail(14).mean()) / float(self.df["close"].iloc[-1]) * 100
        self._factors["atr"] = val
        return val

    def factor_amplitude(self) -> float:
        """当日振幅 %"""
        if self.df.empty:
            return 0
        last = self.df.iloc[-1]
        amp = (last["high"] - last["low"]) / last["close"] * 100
        self._factors["amplitude"] = amp
        return amp

    # ── 量价因子 ─────────────────────────────────────────────────

    def factor_volume_ratio(self) -> float:
        """量比 — 当日成交量 / 5日均量"""
        if len(self.df) < 6:
            return 1.0
        cur_vol = float(self.df["volume"].iloc[-1])
        avg_vol = float(self.df["volume"].tail(6).iloc[:-1].mean())
        val = cur_vol / avg_vol if avg_vol > 0 else 1.0
        self._factors["volume_ratio"] = val
        return val

    def factor_volume_trend(self) -> float:
        """量能趋势 — 近5日成交量线性回归斜率"""
        if len(self.df) < 6:
            return 0
        vol = self.df["volume"].tail(5).values.astype(float)
        try:
            from sklearn.linear_model import LinearRegression
            x = np.arange(len(vol)).reshape(-1, 1)
            model = LinearRegression().fit(x, vol)
            val = float(model.coef_[0]) / (vol.mean() + 1e-9) * 100
        except Exception:
            val = 0
        self._factors["volume_trend"] = val
        return val

    def factor_price_volume_corr(self) -> float:
        """量价相关系数 — 近10日价格与成交量的相关性"""
        if len(self.df) < 11:
            return 0
        val = float(self.df["close"].tail(10).corr(self.df["volume"].tail(10)))
        self._factors["price_volume_corr"] = val
        return val

    def factor_turnover_change(self) -> float:
        """量比加速度 — 当日量比相对于 5 日前量比的变化率。

        量比 = 当日量 / 5日均量，量比加速度 = (今日量比 - 5日前量比) / 5日前量比。
        正值表示放量加速，负值表示缩量。
        与 volume_ratio（量比水平）正交：高量比+正加速=持续放量，高量比+负加速=放量见顶。
        """
        if len(self.df) < 11:
            return 0.0
        cur_vol = float(self.df["volume"].iloc[-1])
        prev_vol = float(self.df["volume"].iloc[-6])
        cur_avg = float(self.df["volume"].tail(6).iloc[:-1].mean())
        prev_avg = float(self.df["volume"].iloc[-11:-1].mean())

        cur_ratio = cur_vol / cur_avg if cur_avg > 0 else 1.0
        prev_ratio = prev_vol / prev_avg if prev_avg > 0 else 1.0

        if prev_ratio > 0:
            val = (cur_ratio - prev_ratio) / prev_ratio * 100
        else:
            val = 0.0
        self._factors["turnover_change"] = val
        return val

    # ── 形态因子 ─────────────────────────────────────────────────

    def factor_ma_slope_5(self) -> float:
        """MA5 斜率（近3日）"""
        if len(self.df) < 8:
            return 0
        ma5 = self.df["close"].rolling(5).mean()
        slope = (ma5.iloc[-1] - ma5.iloc[-4]) / ma5.iloc[-4] * 100
        self._factors["ma_slope_5"] = slope
        return slope

    def factor_ma_slope_10(self) -> float:
        """MA10 斜率"""
        if len(self.df) < 13:
            return 0
        ma10 = self.df["close"].rolling(10).mean()
        slope = (ma10.iloc[-1] - ma10.iloc[-4]) / ma10.iloc[-4] * 100
        self._factors["ma_slope_10"] = slope
        return slope

    def factor_macd_state(self) -> float:
        """MACD 状态: 金叉=2, 多头=1, 空头=-1, 死叉=-2, 零轴上方加分"""
        if len(self.df) < 27:
            return 0
        close = self.df["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()

        state = 0
        if dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]:
            state = 2  # 金叉
        elif dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]:
            state = -2  # 死叉
        elif dif.iloc[-1] > 0 and dif.iloc[-1] > dea.iloc[-1]:
            state = 1  # 多头
        elif dif.iloc[-1] < 0 and dif.iloc[-1] < dea.iloc[-1]:
            state = -1  # 空头

        if dif.iloc[-1] > 0:
            state += 0.5
        self._factors["macd_state"] = state
        return state

    def factor_kdj_state(self) -> float:
        """KDJ 状态: 超卖金叉=3, 金叉=2, 超买死叉=-2。

        使用标准 EMA 平滑公式:
          K_t = 2/3 * K_{t-1} + 1/3 * RSV_t
          D_t = 2/3 * D_{t-1} + 1/3 * K_t
          J_t = 3*K_t - 2*D_t
        初始值 K_0 = D_0 = 50。
        """
        if len(self.df) < 10:
            return 0

        close = self.df["close"].values
        high = self.df["high"].values
        low = self.df["low"].values
        n = 9  # KDJ 标准参数

        k_vals = []
        d_vals = []
        k_prev = 50.0
        d_prev = 50.0

        for i in range(len(self.df)):
            if i < n - 1:
                k_vals.append(50.0)
                d_vals.append(50.0)
                continue
            low_n = low[i - n + 1:i + 1].min()
            high_n = high[i - n + 1:i + 1].max()
            rsv = (close[i] - low_n) / (high_n - low_n + 1e-9) * 100
            k = 2.0 / 3.0 * k_prev + 1.0 / 3.0 * rsv
            d = 2.0 / 3.0 * d_prev + 1.0 / 3.0 * k
            k_vals.append(k)
            d_vals.append(d)
            k_prev, d_prev = k, d

        if len(k_vals) < 2:
            return 0

        k_curr, d_curr = k_vals[-1], d_vals[-1]
        k_prev2, d_prev2 = k_vals[-2], d_vals[-2]

        state = 0
        if k_prev2 <= d_prev2 and k_curr > d_curr and k_curr < 30:
            state = 3  # 超卖金叉
        elif k_prev2 <= d_prev2 and k_curr > d_curr:
            state = 2  # 金叉
        elif k_prev2 >= d_prev2 and k_curr < d_curr and k_curr > 80:
            state = -2  # 超买死叉
        elif k_curr > d_curr:
            state = 1  # 多头
        elif k_curr < d_curr:
            state = -1  # 空头

        self._factors["kdj_state"] = state
        return state

    # ── 资金面因子（基于行情推算） ──────────────────────────────

    def factor_big_order_ratio(self) -> float:
        """
        大单占比估算 — (成交额 - 中单估算) / 成交额
        近似用价格变动 * 成交量突破来判断
        """
        if len(self.df) < 6:
            return 0
        cur_vol = float(self.df["volume"].iloc[-1])
        avg_vol = float(self.df["volume"].tail(6).iloc[:-1].mean())
        cur_pct = float(self.df[self._pct_col].iloc[-1])
        # 放量上涨 = 大单介入
        if cur_vol > avg_vol * 1.3 and cur_pct > 2:
            val = min(1.0, (cur_vol / avg_vol - 1) * cur_pct / 10)
        elif cur_vol < avg_vol * 0.7 and cur_pct > 0:
            val = -0.3  # 缩量上涨，大单参与度低
        else:
            val = 0
        self._factors["big_order_ratio"] = val
        return val

    # ── 综合计算 ─────────────────────────────────────────────────

    ALL_FACTOR_METHODS = [
        factor_mom_1d, factor_mom_5d, factor_mom_10d, factor_mom_20d,
        factor_reversal_3d, factor_mom_ratio,
        factor_volatility_5d, factor_volatility_20d, factor_atr, factor_amplitude,
        factor_volume_ratio, factor_volume_trend, factor_price_volume_corr,
        factor_turnover_change,
        factor_ma_slope_5, factor_ma_slope_10,
        factor_macd_state, factor_kdj_state,
        factor_big_order_ratio,
    ]

    def compute_all(self) -> dict[str, float]:
        """计算所有因子并返回 dict"""
        for method in self.ALL_FACTOR_METHODS:
            try:
                method(self)
            except Exception as e:
                logger.debug("因子 %s 失败: %s", method.__name__, e)
        return dict(self._factors)

    def get_factor(self, name: str) -> float:
        return self._factors.get(name, 0.0)

    def get_all_factors(self) -> dict[str, float]:
        return dict(self._factors)

    # ── 因子标准化与组合评分 ────────────────────────────────────

    @staticmethod
    def standardize(values: list[float]) -> list[float]:
        """z-score 标准化"""
        arr = np.array(values, dtype=float)
        if arr.std() == 0:
            return [0.0] * len(arr)
        return ((arr - arr.mean()) / arr.std()).tolist()

    def composite_score(
        self,
        weights: Optional[dict[str, float]] = None,
        context_factors: Optional[list] = None,
    ) -> float:
        """
        因子组合评分

        Parameters
        ----------
        weights : dict 因子权重, e.g. {"mom_5d": 0.3, "volume_ratio": 0.2}
        context_factors : list 同批股票的因子值列表, 用于截面标准化

        Returns
        -------
        float 综合评分 (0-100)
        """
        if not self._factors:
            self.compute_all()

        # 默认权重（针对短线 2-14 天优化）
        default_weights = {
            "mom_1d": 0.10,
            "mom_5d": 0.12,
            "mom_10d": 0.08,
            "reversal_3d": 0.05,
            "volume_ratio": 0.12,
            "volume_trend": 0.08,
            "price_volume_corr": 0.05,
            "ma_slope_5": 0.10,
            "ma_slope_10": 0.08,
            "macd_state": 0.08,
            "kdj_state": 0.06,
            "big_order_ratio": 0.08,
        }
        w = weights or default_weights

        # 如果有截面数据则用 z-score，否则用线性映射
        score = 0.0
        total_weight = 0.0

        for factor_name, weight in w.items():
            val = self._factors.get(factor_name, 0)
            if context_factors:
                vals = [f.get(factor_name, 0) for f in context_factors]
                z = self.standardize(vals)
                # 使用当前股票的 z-score
                idx = len(context_factors) - 1  # 近似用最后一个
                norm_val = z[-1] if z else 0
            else:
                # 经验映射到 [-1, 1]
                norm_val = self._norm_value(factor_name, val)
            score += norm_val * weight
            total_weight += weight

        if total_weight > 0:
            score /= total_weight

        # 映射到 0-100
        return round((score + 1) / 2 * 100, 1)

    def _norm_value(self, factor_name: str, val: float) -> float:
        """将因子原始值映射到 [-1, 1] 范围"""
        ranges = {
            "mom_1d": (-10, 10),
            "mom_5d": (-40, 40),
            "mom_10d": (-60, 60),
            "mom_20d": (-80, 80),
            "reversal_3d": (-30, 30),
            "mom_ratio": (-40, 40),
            "volatility_5d": (0, 8),
            "volatility_20d": (0, 10),
            "atr": (0, 8),
            "amplitude": (0, 10),
            "volume_ratio": (0, 5),
            "volume_trend": (-10, 10),
            "price_volume_corr": (-1, 1),
            "turnover_change": (-50, 50),
            "ma_slope_5": (-5, 5),
            "ma_slope_10": (-5, 5),
            "macd_state": (-2.5, 2.5),
            "kdj_state": (-3, 3),
            "big_order_ratio": (-1, 1),
        }
        lo, hi = ranges.get(factor_name, (-10, 10))
        if hi == lo:
            return 0
        clipped = max(lo, min(hi, val))
        # 映射到 [-1, 1]
        return 2 * (clipped - lo) / (hi - lo) - 1

    # ── 因子解释 ─────────────────────────────────────────────────

    @staticmethod
    def factor_descriptions() -> dict[str, str]:
        return {
            "mom_1d": "当日涨跌幅",
            "mom_5d": "5日涨跌幅",
            "mom_10d": "10日涨跌幅",
            "mom_20d": "20日涨跌幅",
            "reversal_3d": "3日反转（涨多看空）",
            "mom_ratio": "动量加速度（5日-20日）",
            "volatility_5d": "5日波动率",
            "volatility_20d": "20日波动率",
            "atr": "平均真实波幅占比",
            "amplitude": "当日振幅",
            "volume_ratio": "量比（当日/5日均量）",
            "volume_trend": "量能趋势（5日斜率）",
            "price_volume_corr": "量价相关系数",
            "turnover_change": "量比加速度（5日变化率）",
            "ma_slope_5": "MA5斜率",
            "ma_slope_10": "MA10斜率",
            "macd_state": "MACD状态",
            "kdj_state": "KDJ状态",
            "big_order_ratio": "大单介入估算",
        }

    @staticmethod
    def default_weights_short_term() -> dict[str, float]:
        """短线默认权重"""
        return {
            "mom_1d": 0.10,
            "mom_5d": 0.12,
            "mom_10d": 0.08,
            "reversal_3d": 0.05,
            "volume_ratio": 0.12,
            "volume_trend": 0.08,
            "price_volume_corr": 0.05,
            "ma_slope_5": 0.10,
            "ma_slope_10": 0.08,
            "macd_state": 0.08,
            "kdj_state": 0.06,
            "big_order_ratio": 0.08,
        }