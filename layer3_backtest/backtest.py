"""
回测引擎 — 预缓存 K 线 + 随机采样 + 止损
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from layer1_data import DataFetcher
from layer2_scan.patterns import TechnicalPatterns

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    stock_code: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    hold_days: int
    return_pct: float
    signal_score: int = 0
    signal_names: str = ""


@dataclass
class BacktestConfig:
    start_date: str = "2025-01-01"
    end_date: str = ""
    min_score: int = 2
    holding_days: int = 7
    interval_days: int = 5
    sample_size: int = 500
    top_n: int = 10
    stop_loss: float = -99.0  # 止损幅度（%）
    take_profit: float = 99.0  # 止盈幅度（%）


class BacktestEngine:
    def __init__(self, fetcher: Optional[DataFetcher] = None):
        self.fetcher = fetcher or DataFetcher()

    def run(self, config: BacktestConfig) -> dict:
        end = config.end_date or datetime.now().strftime("%Y-%m-%d")
        logger.info("回测: %s→%s, 持股%d天, min评分≥%d, 止损%.0f%%",
                     config.start_date, end, config.holding_days,
                     config.min_score, config.stop_loss)

        all_stocks = self.fetcher.all_stocks()
        if all_stocks is None or all_stocks.empty:
            return {"error": "无法获取股票列表"}
        codes = all_stocks["stock_code"].tolist()
        random.shuffle(codes)
        codes = codes[:config.sample_size]
        logger.info("采样 %d 只股票", len(codes))

        kline_map = self.fetcher.batch_kline(codes, days=365)
        cache = {k: v for k, v in kline_map.items() if v is not None and len(v) >= 30}
        logger.info("K线缓存: %d/%d", len(cache), len(codes))
        if not cache:
            return {"error": "无可用 K 线数据"}

        start_dt = datetime.strptime(config.start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        trades = []
        current = start_dt

        while current <= end_dt:
            date_str = current.strftime("%Y-%m-%d")
            candidates = []

            for code, kl in cache.items():
                hist = kl[kl["trade_date"] <= date_str]
                if len(hist) < 30:
                    continue
                signals = TechnicalPatterns.scan(hist)
                score = TechnicalPatterns.total_score(signals)
                if score >= config.min_score:
                    candidates.append({
                        "code": code, "price": hist["close"].iloc[-1],
                        "score": score,
                        "signal_names": " | ".join(TechnicalPatterns.signal_names(signals)),
                    })

            candidates.sort(key=lambda x: x["score"], reverse=True)
            for c in candidates[:config.top_n]:
                # 查找持仓期间的 K 线，检查止损止盈
                kl = cache[c["code"]]
                entry_price = c["price"]
                exit_dt = current + timedelta(days=config.holding_days)
                if exit_dt > end_dt:
                    exit_dt = end_dt

                holding = kl[(kl["trade_date"] > date_str) &
                             (kl["trade_date"] <= exit_dt.strftime("%Y-%m-%d"))]
                if holding.empty:
                    continue

                # 检查止损止盈
                exit_price = holding["close"].iloc[-1]
                for _, bar in holding.iterrows():
                    ret = (bar["close"] - entry_price) / entry_price * 100
                    if ret <= config.stop_loss:
                        exit_price = entry_price * (1 + config.stop_loss / 100)
                        break
                    if ret >= config.take_profit:
                        exit_price = entry_price * (1 + config.take_profit / 100)
                        break

                ret = (exit_price - entry_price) / entry_price * 100
                trades.append(TradeRecord(
                    stock_code=c["code"],
                    entry_date=date_str,
                    entry_price=entry_price,
                    exit_date=holding["trade_date"].iloc[-1],
                    exit_price=exit_price,
                    hold_days=config.holding_days,
                    return_pct=ret,
                    signal_score=c["score"],
                    signal_names=c["signal_names"],
                ))

            current += timedelta(days=config.interval_days)

        return self._analyze(trades, config)

    def _analyze(self, trades: list, config: BacktestConfig) -> dict:
        if not trades:
            return {"error": "无交易记录"}
        df = pd.DataFrame([t.__dict__ for t in trades])
        wins = df[df["return_pct"] > 0]
        losses = df[df["return_pct"] <= 0]
        return {
            "start_date": config.start_date,
            "end_date": config.end_date or "now",
            "total_trades": len(trades),
            "win_trades": len(wins), "loss_trades": len(losses),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "avg_return": df["return_pct"].mean(),
            "avg_win": wins["return_pct"].mean() if len(wins) > 0 else 0,
            "avg_loss": losses["return_pct"].mean() if len(losses) > 0 else 0,
        }
