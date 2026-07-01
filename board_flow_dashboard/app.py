#!/usr/bin/env python3
"""
A 股板块资金流向可视化看板 - Flask 后端

提供：
  GET /api/data         → 完整看板数据
  GET /api/data/snapshot → 指定时间点快照（回放用）
  GET /api/status       → 采集器运行状态
  GET /                 → 前端 HTML 页面
  GET /refresh          → 重置采集器
"""

import atexit
import json
import logging
import os
import signal
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from collector import SectorFlowCollector
from data_fetcher import fetch_dashboard_data
from stock_selector import select_stocks

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
    logger.info("实时采集模式 — 采集器已启动")


def _cleanup():
    if collector:
        collector.stop()

atexit.register(_cleanup)

def _signal_handler(signum, frame):
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


@app.route("/api/data")
def api_data():
    if collector is not None:
        data = collector.get_dashboard_data()
    else:
        data = fetch_dashboard_data(use_real=False)
    return jsonify(data)


@app.route("/api/data/snapshot")
def api_snapshot():
    time_idx_str = request.args.get("time", None)
    if collector is not None:
        if time_idx_str is not None:
            data = collector.get_snapshot(int(time_idx_str))
        else:
            data = collector.get_dashboard_data()
    else:
        data = fetch_dashboard_data(use_real=False)
        minutes = data.get("minutes", [])
        series = data.get("series", {})
        if time_idx_str is not None:
            time_idx = int(time_idx_str)
        else:
            time_idx = data.get("time_index", len(minutes) - 1)
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


@app.route("/api/status")
def api_status():
    if collector is not None:
        status = collector.get_status()
        status["mode"] = "live"
    else:
        status = {
            "running": False, "snapshots_count": 0,
            "last_poll_iso": None, "consecutive_failures": 0,
            "market_open": False, "date": "2026-06-30", "mode": "mock",
        }
    return jsonify(status)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/refresh")
def refresh():
    if collector is not None:
        collector.reset()
        return jsonify({"status": "ok", "message": "采集器已重置"})
    return jsonify({"status": "ok", "message": "模拟模式无需重置"})


@app.route("/api/stocks")
def api_stocks():
    """返回盘中选股候选池。"""
    use_mock = _use_mock
    # 如果前端传 ?mock=1 则强制模拟
    if request.args.get("mock", "0") == "1":
        use_mock = True
    data = select_stocks(use_mock=use_mock)
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("启动看板服务: http://127.0.0.1:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
