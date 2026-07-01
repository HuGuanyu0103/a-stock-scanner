#!/usr/bin/env python3
"""
A 股板块资金流向可视化看板 - Flask 后端 v2

提供：
  GET /api/data?type=concept|industry → 板块看板数据（概念/行业）
  GET /api/data/snapshot?time=N&type=concept → 指定时间点快照（回放用）
  GET /api/northbound                 → 北向资金实时数据
  GET /api/stocks/flow                → 全 A 股主力资金流排名
  GET /api/stocks                     → 盘中选股候选池
  GET /api/status                     → 采集器运行状态
  GET /                                → 前端 HTML 页面
  GET /refresh                         → 重置采集器
"""

import atexit
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

try:
    from .collector import SectorFlowCollector
    from .data_fetcher import (
        fetch_dashboard_data, fetch_stock_fund_flow_rank, fetch_northbound_flow, _MOCK_STOCKS,
    )
    from .stock_selector import select_stocks
except ImportError:
    from collector import SectorFlowCollector  # type: ignore[no-redef]
    from data_fetcher import (  # type: ignore[no-redef]
        fetch_dashboard_data, fetch_stock_fund_flow_rank, fetch_northbound_flow, _MOCK_STOCKS,
    )
    from stock_selector import select_stocks  # type: ignore[no-redef]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

_use_mock = "--mock" in sys.argv
if _use_mock:
    logger.info("模拟数据模式 — 不启动采集器")
    collector = None
else:
    collector = SectorFlowCollector(poll_interval=3.0)
    collector.start()
    logger.info("实时采集模式 — 采集器 v2 已启动")


def _cleanup():
    if collector:
        collector.stop()

atexit.register(_cleanup)

def _signal_handler(signum, frame):
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ── 板块资金流向 ────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    """获取板块看板数据。?type=concept（默认）或 industry"""
    sector_type = request.args.get("type", "concept")
    if sector_type not in ("concept", "industry"):
        sector_type = "concept"

    if collector is not None:
        data = collector.get_dashboard_data(sector_type=sector_type)
    else:
        data = fetch_dashboard_data(use_real=False)
        data["sector_type"] = sector_type
    return jsonify(data)


@app.route("/api/data/snapshot")
def api_snapshot():
    """获取指定时间点的快照数据。"""
    time_idx_str = request.args.get("time", None)
    sector_type = request.args.get("type", "concept")
    if sector_type not in ("concept", "industry"):
        sector_type = "concept"

    if collector is not None:
        if time_idx_str is not None:
            data = collector.get_snapshot(int(time_idx_str), sector_type=sector_type)
        else:
            data = collector.get_dashboard_data(sector_type=sector_type)
    else:
        data = fetch_dashboard_data(use_real=False)
        minutes = data.get("minutes", [])
        series = data.get("series", {})
        time_idx = int(time_idx_str) if time_idx_str else data.get("time_index", len(minutes) - 1)
        time_idx = max(0, min(time_idx, len(minutes) - 1))
        time_label = minutes[time_idx] if time_idx < len(minutes) else "15:00"
        snapshot_rank = []
        for sec_name, sec_data in series.items():
            vals = sec_data["values"]
            val = vals[time_idx] if time_idx < len(vals) else (vals[-1] if vals else 0)
            if val is not None:
                snapshot_rank.append({
                    "name": sec_name, "value": val, "color": sec_data["color"],
                })
        snapshot_rank.sort(key=lambda x: x["value"], reverse=True)
        data = {
            "date": data["date"],
            "time_label": time_label,
            "time_index": time_idx,
            "total_times": len(minutes),
            "minutes": minutes,
            "rank": snapshot_rank,
            "series": {
                name: {
                    "name": sdata["name"],
                    "color": sdata["color"],
                    "times": sdata["times"][:time_idx + 1],
                    "values": sdata["values"][:time_idx + 1],
                }
                for name, sdata in series.items()
            },
        }
    return jsonify(data)


# ── 北向资金 ────────────────────────────────────────────────

@app.route("/api/northbound")
def api_northbound():
    """获取北向资金实时/历史数据。"""
    if collector is not None:
        data = collector.get_northbound_data()
    else:
        nb = fetch_northbound_flow()
        data = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "latest": nb or {"time": "--:--", "net_inflow": 0, "hk2sh": 0, "hk2sz": 0},
            "history": {"times": [], "net_inflows": []},
        }
    return jsonify(data)


# ── 个股资金流排名 ──────────────────────────────────────────

@app.route("/api/stocks/flow")
def api_stocks_flow():
    """全 A 股主力资金净流入排名。?sort=net_main|net_ratio&count=50"""
    sort_by = request.args.get("sort", "net_main")
    count = min(int(request.args.get("count", 50)), 200)

    if _use_mock:
        import random
        stocks = []
        for code, name, price, pct, sector in _MOCK_STOCKS[:count]:
            stocks.append({
                "code": code, "name": name,
                "price": price, "pct_chg": pct,
                "net_main": round(random.uniform(-5, 15), 2),
                "net_main_ratio": round(random.uniform(-10, 25), 2),
                "volume_ratio": round(random.uniform(0.3, 3.0), 2),
                "turnover_rate": round(random.uniform(0.5, 10), 1),
            })
        stocks.sort(key=lambda x: x["net_main"], reverse=True)
    else:
        stocks = fetch_stock_fund_flow_rank(sort_by=sort_by, count=count)
        if stocks is None:
            return jsonify({"error": "数据获取失败", "stocks": []}), 500

    now_str = datetime.now().strftime("%H:%M")
    return jsonify({
        "time": now_str,
        "total": len(stocks) if stocks else 0,
        "stocks": stocks or [],
    })


# ── 状态 ────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    if collector is not None:
        status = collector.get_status()
        status["mode"] = "live"
    else:
        status = {
            "running": False,
            "snapshots_concept": 0,
            "snapshots_industry": 0,
            "snapshots_northbound": 0,
            "last_poll_iso": None,
            "consecutive_failures": 0,
            "poll_interval": 0,
            "market_open": False,
            "is_trading_day": False,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "mode": "mock",
        }
    return jsonify(status)


# ── 页面 ────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/refresh")
def refresh():
    if collector is not None:
        collector.reset()
        return jsonify({"status": "ok", "message": "采集器已重置"})
    return jsonify({"status": "ok", "message": "模拟模式无需重置"})


# ── 盘中选股 ────────────────────────────────────────────────

@app.route("/api/stocks")
def api_stocks():
    """返回盘中选股候选池。"""
    use_mock = _use_mock
    if request.args.get("mock", "0") == "1":
        use_mock = True
    data = select_stocks(use_mock=use_mock, collector=collector)
    return jsonify(data)


# ── 日评分管理 ──────────────────────────────────────────────

@app.route("/api/daily-scorer/run")
def api_daily_scorer_run():
    """手动触发日评分扫描。"""
    try:
        try:
            from .daily_scorer import run_scan
        except ImportError:
            from daily_scorer import run_scan  # type: ignore[no-redef]
        scores = run_scan(top_n=300)
        return jsonify({
            "status": "ok",
            "count": len(scores),
            "message": f"扫描完成，缓存 {len(scores)} 只股票",
        })
    except Exception as e:
        logger.error("日评分扫描失败: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/daily-scorer/status")
def api_daily_scorer_status():
    """查看日评分缓存状态。"""
    try:
        try:
            from .daily_scorer import load_daily_scores, cache_path
        except ImportError:
            from daily_scorer import load_daily_scores, cache_path  # type: ignore[no-redef]
        cp = cache_path()
        exists = cp.exists()
        scores = load_daily_scores() if exists else {}
        return jsonify({
            "cache_exists": exists,
            "cache_file": str(cp),
            "stock_count": len(scores),
            "avg_score": round(sum(v["combined_score"] for v in scores.values()) / len(scores), 1)
                         if scores else 0,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── 启动时自动跑日评分 ──────────────────────────────────────

def _auto_run_daily_scorer():
    """交易日上午 9:25 后启动时，自动执行日评分扫描。"""
    now = datetime.now()
    # 只在交易日的 9:25-9:35 之间自动触发
    if not _use_mock:
        try:
            try:
                from .data_fetcher import is_trading_day
                from .daily_scorer import cache_path, run_scan
            except ImportError:
                from data_fetcher import is_trading_day  # type: ignore[no-redef]
                from daily_scorer import cache_path, run_scan  # type: ignore[no-redef]

            if is_trading_day(now.date()) and now.hour == 9 and 25 <= now.minute <= 35:
                if not cache_path().exists():
                    logger.info("交易日 %s 9:25+，自动执行日评分扫描...", now.strftime("%Y-%m-%d"))
                    scores = run_scan(top_n=300)
                    logger.info("自动日评分完成: %d 只股票", len(scores))
        except Exception as e:
            logger.warning("自动日评分失败: %s", e)


_auto_run_daily_scorer()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("启动看板服务 v2: http://127.0.0.1:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
