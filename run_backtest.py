#!/usr/bin/env python3
"""
策略回测入口

python3 run_backtest.py
python3 run_backtest.py --holding 7 --min-score 3
python3 run_backtest.py --start 2025-01-01 --end 2025-06-01
python3 run_backtest.py -i 5 --sample 200
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

import yaml

from layer3_backtest import BacktestEngine, BacktestConfig


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("adata").setLevel(logging.WARNING)


def load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def main():
    parser = argparse.ArgumentParser(description="A 股策略回测")
    parser.add_argument("--holding", type=int, default=0, help="持股天数")
    parser.add_argument("--top", type=int, default=0, help="每次选 Top N")
    parser.add_argument("--min-score", type=int, default=0, help="最低信号评分")
    parser.add_argument("--start", type=str, default="", help="回测开始日期")
    parser.add_argument("--end", type=str, default="", help="回测结束日期")
    parser.add_argument("-i", "--interval", type=int, default=0, help="扫描间隔天数")
    parser.add_argument("--stop-loss", type=float, default=0, help="止损百分比")
    parser.add_argument("--take-profit", type=float, default=0, help="止盈百分比")
    parser.add_argument("--sample", type=int, default=0, help="采样股票数量")
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--log-level", type=str, default="")

    args = parser.parse_args()
    config = load_config(args.config)
    bc = config.get("backtest", {})

    log_level = args.log_level or config.get("logging", {}).get("level", "INFO")
    setup_logging(log_level)

    bt_config = BacktestConfig(
        start_date=args.start or bc.get("start_date", "2025-01-01"),
        end_date=args.end or bc.get("end_date", ""),
        holding_days=args.holding or bc.get("holding_days", 7),
        min_score=args.min_score or bc.get("min_score", 2),
        interval_days=args.interval or bc.get("interval_days", 5),
        sample_size=args.sample or bc.get("sample_size", 500),
        top_n=args.top or bc.get("top_n", 10),
        stop_loss=args.stop_loss or bc.get("stop_loss", -99.0),
        take_profit=args.take_profit or bc.get("take_profit", 99.0),
    )

    engine = BacktestEngine()
    result = engine.run(bt_config)

    if "error" in result:
        print(f"回测失败: {result['error']}")
        return

    print(f"\n{'='*55}")
    print(f"  回测结果")
    print(f"  期间: {result['start_date']} → {result['end_date']}")
    print(f"  持股: {bt_config.holding_days}天  间隔: {bt_config.interval_days}天")
    print(f"  min评分: {bt_config.min_score}  采样: {bt_config.sample_size}只")
    print(f"{'='*55}")
    print(f"  交易次数:     {result['total_trades']}")
    print(f"  胜率:         {result['win_rate']:.1f}%")
    print(f"  平均收益:     {result['avg_return']:+.2f}%")
    print(f"  平均盈利:     {result['avg_win']:+.2f}%")
    print(f"  平均亏损:     {result['avg_loss']:+.2f}%")


if __name__ == "__main__":
    main()
