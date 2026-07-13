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
import re
from typing import Optional
import signal
import sys
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path 中（用于导入 layer1_data 等模块）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, stream_with_context

try:
    from .collector import SectorFlowCollector, WATCH_SECTORS
    from .data_fetcher import (
        fetch_dashboard_data, fetch_stock_fund_flow_rank, fetch_northbound_flow, _MOCK_STOCKS, _now_time,
    )
    from .agent import get_agent
    from .decision_store import get_decision_store
    from .loop_analyzer import get_loop_analyzer
    from .stock_selector import select_stocks
    from .signals import get_signal_store, compute_final_score, get_pool_allocation
    from .daily_scorer import load_daily_scores
    from .sentiment_collector import SentimentCollector
    from .news_monitor import NewsMonitor
    from .pre_market import PreMarketScanner
except ImportError:
    from collector import SectorFlowCollector, WATCH_SECTORS  # type: ignore[no-redef]
    from data_fetcher import (  # type: ignore[no-redef]
        fetch_dashboard_data, fetch_stock_fund_flow_rank, fetch_northbound_flow, _MOCK_STOCKS, _now_time,
    )
    from agent import get_agent  # type: ignore[no-redef]
    from decision_store import get_decision_store  # type: ignore[no-redef]
    from loop_analyzer import get_loop_analyzer
    from debate import get_orchestrator  # type: ignore[no-redef]
    from stock_selector import select_stocks  # type: ignore[no-redef]
    from signals import get_signal_store, compute_final_score, get_pool_allocation  # type: ignore[no-redef]
    from sentiment_collector import SentimentCollector  # type: ignore[no-redef]
    from news_monitor import NewsMonitor  # type: ignore[no-redef]
    from pre_market import PreMarketScanner  # type: ignore[no-redef]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Gzip 压缩（含静态文件），减少 Cloudflare Tunnel 带宽
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    @app.after_request
    def _gzip_response(response):
        accept = request.headers.get('Accept-Encoding', '')
        if 'gzip' in accept and response.content_length and response.content_length > 1024:
            import gzip, io
            response.direct_passthrough = False
            gzip_buffer = io.BytesIO()
            gzip_file = gzip.GzipFile(mode='wb', fileobj=gzip_buffer, compresslevel=4)
            gzip_file.write(response.get_data())
            gzip_file.close()
            response.set_data(gzip_buffer.getvalue())
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(len(response.get_data()))
        return response

# 静态文件缓存头（echarts.min.js 1MB 缓存 7 天）
@app.after_request
def _cache_static(response):
    if request.path.startswith('/static/'):
        ext = request.path.rsplit('.', 1)[-1] if '.' in request.path else ''
        if ext in ('js', 'woff2', 'woff'):
            response.headers['Cache-Control'] = 'public, max-age=604800'
        elif ext in ('css', 'png', 'svg'):
            response.headers['Cache-Control'] = 'public, max-age=86400'
    return response

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

# 始终启动实时采集器，永不使用模拟数据
collector = SectorFlowCollector(poll_interval=3.0)
collector.start()
logger.info("实时采集模式 — 采集器已启动")

# System B: 情绪面采集器（独立线程）
sentiment_collector = SentimentCollector()
sentiment_collector.start()
logger.info("System B（情绪面）已启动")

# System C: 消息面监控（独立线程）
news_monitor = NewsMonitor()
news_monitor.start()
logger.info("System C（消息面）已启动")

# v4.0: 盘前预扫描
pre_market_scanner = PreMarketScanner()

# 启动信号定期持久化（每 60s）
get_signal_store().start_auto_persist(interval=60)


def _cleanup():
    if collector:
        collector.stop()
    if sentiment_collector:
        sentiment_collector.stop()
    if news_monitor:
        news_monitor.stop()
    # 持久化信号存储
    try:
        get_signal_store().persist()
    except Exception:
        pass

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
    sector_type = request.args.get("type", "watch")
    if sector_type not in ("concept", "industry", "watch"):
        sector_type = "watch"
    date_param = request.args.get("date", "").strip()

    if date_param:
        data = collector.get_dashboard_data_for_date(date_param, sector_type=sector_type)
    else:
        data = collector.get_dashboard_data(sector_type=sector_type)
    return jsonify(data)


@app.route("/api/dates")
def api_dates():
    """返回 DB 中有数据的交易日期列表，供前端日期选择器。"""
    dates = collector.get_available_dates() if collector else []
    return jsonify({"dates": dates})


@app.route("/api/data/snapshot")
def api_snapshot():
    """获取指定时间点的快照数据。"""
    time_idx_str = request.args.get("time", None)
    sector_type = request.args.get("type", "watch")
    if sector_type not in ("concept", "industry", "watch"):
        sector_type = "watch"

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


# ── 板块分时切片 ────────────────────────────────────────────

@app.route("/api/sector/timeseries")
def api_sector_timeseries():
    """获取指定板块最近 N 分钟的资金流向时间序列。
    ?name=光通信模块&minutes=30
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "请指定板块名称"}), 400
    minutes = min(int(request.args.get("minutes", 30)), 120)
    data = collector.get_sector_timeseries(name, minutes)
    return jsonify(data)


# ── 个股资金流排名 ──────────────────────────────────────────

@app.route("/api/stocks/flow")
def api_stocks_flow():
    """全 A 股主力资金净流入排名。?sort=net_main|net_ratio&count=50"""
    sort_by = request.args.get("sort", "net_main")
    count = min(int(request.args.get("count", 50)), 200)

    stocks = fetch_stock_fund_flow_rank(sort_by=sort_by, count=count)
    if stocks is None:
        return jsonify({"error": "数据获取失败", "time": _now_time(), "stocks": []}), 500

    return jsonify({
        "time": _now_time(),
        "total": len(stocks) if stocks else 0,
        "stocks": stocks or [],
    })


# ── 状态 ────────────────────────────────────────────────────

def _is_market_open_now():
    """判断当前是否在A股交易时间内"""
    from datetime import time as dt_time
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    morning_start = dt_time(9, 30)
    morning_end = dt_time(11, 30)
    afternoon_start = dt_time(13, 0)
    afternoon_end = dt_time(15, 0)
    t = now.time()
    return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)


@app.route("/api/status")
def api_status():
    status = collector.get_status()
    status["mode"] = "live"
    # 暴露熔断器状态
    try:
        from .data_fetcher import get_circuit_status
    except ImportError:
        from data_fetcher import get_circuit_status  # type: ignore[no-redef]
    status["circuit_breakers"] = get_circuit_status()
    return jsonify(status)


# ── 页面 ────────────────────────────────────────────────────

@app.route("/")
def index():
    # 如果请求没有版本参数，重定向到带版本号的 URL，强制浏览器加载最新版
    if not request.args.get('v'):
        import time
        return redirect(f"/?v={int(time.time())}", code=302)
    response = send_from_directory(STATIC_DIR, "index.html")
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route("/test")
def test_page():
    return send_from_directory(STATIC_DIR, "test.html")

@app.route("/refresh")
def refresh():
    collector.reset()
    return jsonify({"status": "ok", "message": "采集器已重置"})


# ── 盘中选股 + 多系统聚合 ──────────────────────────────────

@app.route("/api/stocks")
def api_stocks():
    """返回盘中选股候选池（多系统聚合）。

    System A（技术+资金）提供基础分，System B（情绪）调节策略基调，
    System C（消息）提供事件加分。降级：任何系统挂了自动分权。
    """
    data = select_stocks(collector=collector)

    # 多系统聚合
    store = get_signal_store()
    signals = store.get_all()
    weights = store.get_effective_weights()
    pool_alloc = get_pool_allocation(signals)

    # 对每只候选股计算聚合分
    for pool_key in ("pool_a", "pool_b"):
        for stock in data.get(pool_key, []):
            stock["base_score"] = stock.get("score", 0)
            stock["aggregated_score"] = compute_final_score(stock, signals, weights)
            stock["signal_sources"] = {
                k: v["status"] for k, v in signals.items()
            }

    # 按聚合分重排
    for pool_key in ("pool_a", "pool_b"):
        data[pool_key].sort(key=lambda x: x.get("aggregated_score", 0), reverse=True)

    data["signal_weights"] = weights
    data["pool_allocation"] = pool_alloc
    data["signal_status"] = {
        k: {"status": v["status"], "updated_at": v["updated_at"]}
        for k, v in signals.items()
    }

    # v4.0: 情绪面摘要（供前端展示）
    sent_data = signals.get("sentiment", {}).get("data", {})
    market = sent_data.get("market", {})
    data["sentiment"] = {
        "sentiment_index": market.get("sentiment_index"),
        "po_ban_rate": market.get("po_ban_rate"),
        "rotation_speed": market.get("rotation_speed"),
        "lianban_rate": market.get("lianban_rate"),
    }

    # v4.0: 盘前外盘映射 + A池施加
    pre_market = pre_market_scanner.scan()
    data["pre_market"] = {
        "overall_sentiment": pre_market.get("overnight", {}).get("overall_sentiment", 0),
        "summary": pre_market.get("summary", ""),
        "positive_sectors": pre_market.get("overnight", {}).get("positive_sectors", []),
        "negative_sectors": pre_market.get("overnight", {}).get("negative_sectors", []),
        "indices": pre_market.get("overnight", {}).get("indices", {}),
    }
    # 盘前情绪施加到 A 池
    if pre_market.get("pre_market_open"):
        data["pool_a"] = pre_market_scanner.apply_to_candidates(
            data["pool_a"], pool_type="A")

    return jsonify(data)


@app.route("/api/pre-market")
def api_pre_market():
    """v4.0: 盘前预扫描数据（隔夜外盘 + 集合竞价）。"""
    result = pre_market_scanner.scan()
    return jsonify(result)


@app.route("/api/signals")
def api_signals():
    """多系统信号健康状态 + 情绪指数。"""
    store = get_signal_store()
    signals = store.get_all()
    return jsonify({
        "signals": {
            k: {"status": v["status"], "updated_at": v["updated_at"]}
            for k, v in signals.items()
        },
        "effective_weights": store.get_effective_weights(),
        "sentiment": signals.get("sentiment", {}).get("data", {}).get("market", {}),
        "pool_allocation": get_pool_allocation(signals),
        "news_events": signals.get("news", {}).get("data", {}).get("events", [])[:10],
    })


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


# ── AI Agent 端点 ─────────────────────────────────────────────

@app.route("/api/agent/intraday")
def api_agent_intraday():
    """生成盘中 AI 决策建议。"""
    data = select_stocks(collector=collector)
    all_candidates = data.get("pool_a", []) + data.get("pool_b", [])
    if not all_candidates:
        return jsonify({"error": "候选池为空", "mode": "empty"})

    store = get_signal_store()
    signals = store.get_all()
    breadth = data.get("market_breadth", 0.5)
    hot_sectors = data.get("hot_sectors", [])

    agent = get_agent()
    result = agent.generate_intraday_picks(
        all_candidates, hot_sectors, signals, breadth,
    )
    if result:
        result["mode"] = data.get("mode", "live")
        result["risk_level"] = data.get("risk_level", "low")
        return jsonify(result)
    return jsonify({
        "error": "Agent 不可用",
        "mode": data.get("mode", "live"),
        "fallback_hint": "请查看盘中选股标签页的候选池",
    })


@app.route("/api/agent/chat", methods=["POST"])
def api_agent_chat():
    """多轮对话：用户追问 AI Agent。"""
    body = request.get_json(silent=True) or {}
    user_message = body.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "消息不能为空"}), 400

    chat_history = body.get("history", [])

    # 前端预加载的诊断数据（如果用户消息含股票代码且前端已调 /api/stock/diagnose）
    diagnose_data = body.get("diagnose_data")

    data = select_stocks(collector=collector)
    all_candidates = data.get("pool_a", []) + data.get("pool_b", [])
    store = get_signal_store()
    signals = store.get_all()
    hot_sectors = data.get("hot_sectors", [])

    agent = get_agent()
    # 检测用户消息中的股票代码并获取实时数据
    stock_data_context, kline_data = _extract_stock_context(user_message, diagnose_data=diagnose_data)

    # 安全网：如果消息含股票代码但数据获取失败，直接返回错误，防止 LLM 编造数据
    stock_code_in_msg = _extract_stock_code(user_message)
    logger.info("Chat: code=%s, ctx_len=%d, kline_len=%d, msg=%.80s",
                stock_code_in_msg, len(stock_data_context), len(kline_data), user_message)
    if stock_code_in_msg and not stock_data_context:
        return jsonify({
            "reply": (
                f"⚠️ 无法获取 {stock_code_in_msg} 的实时数据。\n\n"
                f"可能原因：1. 数据接口超时 2. 股票代码错误 3. 非交易时段数据未更新\n\n"
                f"建议：稍后重试，或查看该股票所属板块的整体表现。"
            ),
            "kline": "",
        })

    # 检测用户消息中的板块名称并获取资金流时序数据
    sector_ts_context = ""
    for sector in WATCH_SECTORS:
        if sector in user_message:
            ts = collector.get_sector_timeseries(sector, minutes=30)
            if "error" not in ts:
                sector_ts_context = json.dumps(ts, ensure_ascii=False)
            break

    reply = agent.chat(
        user_message, all_candidates, hot_sectors, signals, chat_history,
        stock_context=stock_data_context,
        sector_timeseries=sector_ts_context,
    )
    if reply:
        return jsonify({
            "reply": reply,
            "kline": kline_data,
            "_ts": datetime.now().strftime("%H:%M:%S"),
            "_code": stock_code_in_msg or "",
            "_ctx": len(stock_data_context),
        })
    return jsonify({"error": "Agent 不可用", "reply": "抱歉，AI 助手暂时不可用，请稍后重试。",
                    "_ts": datetime.now().strftime("%H:%M:%S")})


@app.route("/api/agent/chat/stream", methods=["POST"])
def api_agent_chat_stream():
    """SSE 流式对话端点 — 逐 token 推送，消除 8-10 秒等待。

    前端通过 EventSource/fetch+ReadableStream 接收，首字延迟 < 2 秒。
    同时推送 kline_table 和诊断卡片数据，支持渐进式 UI 渲染。
    """
    body = request.get_json(silent=True) or {}
    user_message = body.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "消息不能为空"}), 400

    chat_history = body.get("history", [])
    diagnose_data = body.get("diagnose_data")

    data = select_stocks(collector=collector)
    all_candidates = data.get("pool_a", []) + data.get("pool_b", [])
    store = get_signal_store()
    signals = store.get_all()
    hot_sectors = data.get("hot_sectors", [])

    agent = get_agent()
    stock_data_context, kline_data = _extract_stock_context(user_message, diagnose_data=diagnose_data)

    stock_code_in_msg = _extract_stock_code(user_message)
    if stock_code_in_msg and not stock_data_context:
        return jsonify({
            "reply": f"⚠️ 无法获取 {stock_code_in_msg} 的实时数据。请稍后重试。",
            "kline": "",
        })

    # 板块时序上下文
    sector_ts_context = ""
    for sector in WATCH_SECTORS:
        if sector in user_message:
            ts = collector.get_sector_timeseries(sector, minutes=30)
            if "error" not in ts:
                sector_ts_context = json.dumps(ts, ensure_ascii=False)
            break

    def generate():
        ts = datetime.now().strftime("%H:%M:%S")
        ctx_len = len(stock_data_context)

        # Phase 1: 推送元数据（K线表格 + 基本信息，前端立即渲染卡片壳）
        yield f"data: {json.dumps({'type': 'meta', 'kline': kline_data, '_ts': ts, '_code': stock_code_in_msg or '', '_ctx': ctx_len}, ensure_ascii=False)}\n\n"

        # Phase 2: 流式推送 LLM 输出
        full_reply = ""
        try:
            for chunk in agent.chat_stream(
                user_message, all_candidates, hot_sectors, signals,
                chat_history, stock_data_context, sector_ts_context,
            ):
                if chunk is None:
                    break
                full_reply += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'data': chunk}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error("SSE stream error: %s", e)

        # Phase 3: 完成信号
        yield f"data: {json.dumps({'type': 'done', '_ts': ts, '_code': stock_code_in_msg or '', '_ctx': ctx_len}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# ── 个股分析端点 ──────────────────────────────────────────────

def _get_daily_score_info(code: str) -> Optional[dict]:
    """获取日级别评分 + 全市场排名信息。"""
    try:
        scores = load_daily_scores()
        if not scores or code not in scores:
            return None
        entry = scores[code]
        # 计算排名
        sorted_scores = sorted(scores.values(), key=lambda x: x["combined_score"], reverse=True)
        rank = next(i + 1 for i, s in enumerate(sorted_scores) if s.get("combined_score") == entry["combined_score"])
        total = len(sorted_scores)
        return {
            **entry,
            "rank": rank,
            "rank_pct": round(rank / total * 100, 1),
            "total": total,
        }
    except Exception:
        return None


def _extract_stock_code(text):
    """从文本中提取6位股票代码"""
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
    return m.group(1) if m else None

def _analyze_kline_events(df):
    """分析K线历史，自动标注关键事件。返回 {行序号: [事件标签]} 和统计摘要。"""
    if df is None or len(df) < 5:
        return {}, {}
    closes = df['close'].values.astype(float)
    opens  = df['open'].values.astype(float)
    highs  = df['high'].values.astype(float)
    lows   = df['low'].values.astype(float)
    volumes = df['volume'].values.astype(float)
    n = len(df)

    low_20  = lows[-20:].min()
    avg_vol_20 = volumes[-20:].mean() if n >= 20 else volumes.mean()
    max_vol_20 = volumes[-20:].max() if n >= 20 else volumes.max()

    annotations = {}
    # 计算 20 日平均成交额（亿元）
    avg_amount = round(float((volumes[-20:] * closes[-20:] / 1e8).mean()), 1) if n >= 20 else 0
    max_amount = round(float((volumes[-20:] * closes[-20:] / 1e8).max()), 1) if n >= 20 else 0

    stats = {
        'low_20': round(float(low_20), 2),
        'high_20': round(float(highs[-20:].max()), 2),
        'avg_amount': avg_amount,
        'max_amount': max_amount,
        'pct_from_low': round(float((closes[-1] - low_20) / low_20 * 100), 1),
    }

    for i in range(max(5, n - 20), n):
        o, c, h, l = opens[i], closes[i], highs[i], lows[i]
        v = volumes[i]
        vol_r = v / avg_vol_20 if avg_vol_20 > 0 else 1
        chg = (c - o) / o * 100 if o > 0 else 0
        tags = []

        if c <= low_20 * 1.05:
            tags.append('底部区域')
        if vol_r > 1.8 and chg > 0:
            tags.append('放量启动')
        if v >= max_vol_20 * 0.95:
            tags.append('天量')
        if i >= 2:
            c1 = (closes[i-1] - opens[i-1]) / opens[i-1] * 100 if opens[i-1] > 0 else 0
            c2 = (closes[i-2] - opens[i-2]) / opens[i-2] * 100 if opens[i-2] > 0 else 0
            if chg > 3 and chg > c1 and c1 > 0:
                tags.append('加速')
        if chg < -3 and vol_r < 1.5:
            tags.append('洗盘')
        if i >= 1 and chg > 0:
            po, pc = opens[i-1], closes[i-1]
            pcg = (pc - po) / po * 100 if po > 0 else 0
            if pcg < 0 and o <= pc and c >= po:
                tags.append('反包')

        if tags:
            annotations[i] = tags

    return annotations, stats


def _build_daily_fallback_context(code: str) -> str:
    """当日K线/实时数据获取失败时，用日评分数据兜底。"""
    ds = _get_daily_score_info(code)
    if not ds:
        return ""
    return (
        f"=== {code} 日级别评分（盘后全市场扫描，实时K线暂不可用）===\n"
        f"日综合评分: {ds['combined_score']}\n"
        f"全市场排名: #{ds['rank']}/{ds['total']} (前{ds['rank_pct']}%)\n"
        f"技术信号: {ds['signal_score']}分 | 情绪: {ds['sentiment_score']}分 | 因子: {ds['factor_score']}分\n"
        f"触发信号: {ds['signal_names']}\n"
        f"\n[以上为盘后静态评分，实时行情暂不可用。请基于此数据和你的交易经验给出分析，"
        f"标注数据来源。禁止编造价格、涨跌幅等实时数据。]"
    )


def _build_context_from_diagnose(data: dict) -> str:
    """从诊断 JSON 构建 LLM 上下文（精简版，避免重复 API 调用）。

    与 _extract_stock_context 不同：该函数直接使用已计算好的诊断数据，
    省去 quick_scan + analyze_stock + turnover_rate 三次 API 调用。
    上下文长度从 ~80 行压缩到 ~40 行，去除与 SYSTEM_PROMPT 重复的指令。
    """
    code = data.get("code", "")
    name = data.get("name", "")
    price = data.get("price")
    pct = data.get("change_pct") or 0
    vol_ratio = data.get("volume_ratio")
    turnover = data.get("turnover_rate")
    amp = data.get("amp")
    intraday = data.get("intraday")
    intra_label = "高位" if (intraday or 0) > 0.7 else ("中位" if (intraday or 0) > 0.3 else "低位")
    mas = data.get("mas", {})
    concepts = data.get("concepts", [])
    signals = data.get("signals", [])
    factors = data.get("factors", {})
    sentiment = data.get("sentiment", {})
    news = data.get("news", [])
    ds = data.get("daily_score")
    kline_stats = data.get("kline_stats", {})

    parts = [
        f"=== {code} {name} 实时诊断 ===",
        "",
        "▸ 量价数据",
        f"现价 {price or '?'}  涨跌 {pct:+.1f}%  量比 {vol_ratio or '?'}  "
        f"换手率 {f'{turnover}%' if turnover is not None else '?'}  "
        f"振幅 {f'{amp}%' if amp is not None else '?'}  "
        f"日内位置 {intraday or '?'}({intra_label})",
        "",
        "▸ 技术研判",
        f"MA5 {mas.get('ma5', 0):.2f}  MA10 {mas.get('ma10', 0):.2f}  MA20 {mas.get('ma20', 0):.2f}",
        f"支撑 {data.get('support', '?')}  阻力 {data.get('resistance', '?')}",
    ]
    if kline_stats:
        parts.append(
            f"20日最低 {kline_stats.get('low_20','?')}  20日最高 {kline_stats.get('high_20','?')}  "
            f"20日均额 {kline_stats.get('avg_amount','?')}亿  距底 {kline_stats.get('pct_from_low','?')}%"
        )
    parts.append(f"趋势: {data.get('summary', '')}")

    parts.extend(["", "▸ 触发信号"])
    if signals:
        for s in signals[:6]:
            lv = s.get('level', 1)
            stars = '★' * min(lv, 5) + '☆' * max(0, 5 - min(lv, 5))
            desc = s.get('desc', '')
            parts.append(f"  [{stars}] {s['name']}{' — ' + desc if desc else ''}")
    else:
        parts.append("  (无)")

    if factors:
        parts.append("")
        parts.append("▸ 量化因子(Top8)")
        sorted_f = sorted(factors.items(), key=lambda x: abs(x[1]), reverse=True)[:8]
        for k, v in sorted_f:
            bar = '█' * min(10, int(abs(v) / 3)) if abs(v) > 0 else ''
            arrow = '↑' if v > 0 else ('↓' if v < 0 else '→')
            parts.append(f"  {k}: {v:+.1f} {arrow} {bar}")

    if concepts:
        parts.extend(["", "▸ 概念属性", ', '.join(concepts)])

    if sentiment and sentiment.get('index') is not None:
        parts.extend([
            "", "▸ 市场情绪",
            f"情绪指数: {sentiment.get('index','?')}  "
            f"炸板率: {round((sentiment.get('po_ban_rate') or 0) * 100)}%  "
            f"轮动速度: {sentiment.get('rotation','?')}",
        ])

    if news:
        parts.extend(["", "▸ 相关消息"])
        for ev in news[:3]:
            parts.append(f"  [{ev.get('sentiment', 'neutral')}] {ev.get('title', '')}")

    if ds:
        parts.extend([
            "", "▸ 日级别评分(盘后全市场扫描)",
            f"日综合{ds['combined_score']} | 技术{ds['signal_score']}+情绪{ds['sentiment_score']}+因子{ds['factor_score']}",
            f"全市场排名 #{ds['rank']}/{ds['total']}(前{ds['rank_pct']}%) | 信号: {ds['signal_names']}",
        ])

    parts.extend([
        "", "▸ 综合评分",
        f"综合{data.get('combined_score', '?')} | 池{data.get('pool', '未入池')}",
    ])

    # K线关键事件摘要（完整标注已在UI的K线表格中展示）
    kline_events = data.get("kline_events", [])
    if kline_events:
        parts.extend(["", "▸ K线关键事件（已标注在UI表格中）"])
        for ev in kline_events[:8]:
            parts.append(f"  {ev['date']}: {' '.join(ev['tags'])}")

    parts.extend([
        "",
        "K线表格已在UI中展示，分析时引用具体日期和标注事件。",
        "输出结构: K线量价分析→量价数据→技术研判→资金面→综合评分→操作建议→一句话。",
        "每项分析要有具体数字。操作建议用严格格式:",
        "入场区间: X.XX-X.XX",
        "目标: X.XX",
        "止损: X.XX",
        "仓位: 轻仓(1-2成)/中仓(3-4成)/重仓(5成+)",
        "持有周期: X天",
        "风险等级: 低/中/高",
        "禁止说「数据不足」「无法获取」。禁止使用emoji。",
    ])

    return '\n'.join(parts)


def _extract_stock_context(user_message, diagnose_data: Optional[dict] = None):
    """返回 (LLM上下文, K线表格数据) 的元组。

    当 diagnose_data 可用时（前端已调用 /api/stock/diagnose 预加载），
    直接从诊断数据构建上下文，避免重复调用 quick_scan + analyze_stock。
    """
    code = _extract_stock_code(user_message)
    if not code:
        logger.info("StockContext: 未提取到股票代码, msg=%.60s", user_message)
        return "", ""

    # 捷径：使用前端预加载的诊断数据，跳过 API 调用
    if diagnose_data and not diagnose_data.get("error") and diagnose_data.get("code") == code:
        kline_table = diagnose_data.get("kline_table", "")
        if kline_table:
            logger.info("StockContext: 使用预加载诊断数据 %s, kline_table=%d chars",
                        code, len(kline_table))
            ctx = _build_context_from_diagnose(diagnose_data)
            return ctx, kline_table
        # 数据不完整，回退到 API 路径
        logger.info("StockContext: 诊断数据无 kline_table，回退 API %s", code)

    try:
        from layer1_data.fetcher import DataFetcher
        from layer2_scan.screener import StockScreener
        from layer4_analysis.analyzer import StockAnalyzer
        fetcher = DataFetcher()
        screener = StockScreener(fetcher=fetcher)
        analyzer = StockAnalyzer(fetcher=fetcher)

        scan = screener.quick_scan(code)
        logger.info("StockContext: quick_scan %s -> %s", code,
                     "ERROR:" + scan.get("error","") if "error" in scan else f"OK kline={len(scan.get('kline',[]))}rows")
        shared_kline = scan.get("kline") if "error" not in scan else None
        deep = analyzer.analyze_stock(code, kline=shared_kline)
        logger.info("StockContext: analyze_stock %s -> %s", code,
                     "ERROR:" + deep.get("error","") if "error" in deep else f"OK price={deep.get('price')}")

        # K线表格（带关键事件自动标注）
        kline_csv = ""
        kline = scan.get("kline") if "error" not in scan else None
        annotations = {}
        kline_stats = {}

        kline_nrows = len(kline) if kline is not None and hasattr(kline, '__len__') else 0
        if kline is not None and hasattr(kline, 'tail') and kline_nrows >= 5:
            annotations, kline_stats = _analyze_kline_events(kline)
            recent = kline.tail(18)

            kline_csv  = "  日期     开盘   收盘    涨幅%     成交额      \n"
            kline_csv += "──────────────────────────────────────────\n"
            for row_idx, (_, row) in enumerate(recent.iterrows()):
                td = row.get('trade_date')
                if td is None and hasattr(row, 'name'):
                    td = row.name
                try:
                    td_str = str(td)[:10]
                    if '-' in td_str and len(td_str) >= 10:
                        parts = td_str.split('-')
                        date_str = f"{parts[1]}/{parts[2]}"
                    elif '/' in td_str:
                        date_str = td_str[-5:] if len(td_str) >= 5 else td_str
                    else:
                        date_str = td_str
                except Exception:
                    date_str = str(td)[:5]

                o = float(row.get('open', 0))
                c = float(row.get('close', 0))
                chg = ((c - o) / o * 100) if o > 0 else 0
                # 成交额（亿）= 成交量（股）/ 1e8 × 均价近似收盘价，但直接用量价估算会偏大
                # 用收盘价×成交量/1e8 估算成交额
                vol_shares = float(row.get('volume', 0))
                amount = vol_shares * c / 1e8  # 成交额（亿元）

                global_idx = kline_nrows - len(recent) + row_idx
                tags = annotations.get(global_idx, [])
                tag_str = ('  ← ' + ' '.join(tags)) if tags else ''

                kline_csv += (
                    f"  {date_str}  {o:>7.2f} {c:>7.2f}  "
                    f"{chg:>+6.1f}%  {amount:>5.0f}亿{tag_str}\n"
                )
            kline_csv += "──────────────────────────────────────────\n"
            kline_csv += f"  20日最低 {kline_stats.get('low_20','?')}  "
            kline_csv += f"20日最高 {kline_stats.get('high_20','?')}  "
            kline_csv += f"20日均额 {kline_stats.get('avg_amount','?')}亿  "
            kline_csv += f"距底 {kline_stats.get('pct_from_low','?')}%"

        if "error" in scan:
            fallback = _build_daily_fallback_context(code)
            if fallback:
                logger.info("StockContext: 实时数据不可用，日评分兜底 %s", code)
                return fallback, ""
            return (
                f"[用户询问股票 {code}，实时数据接口暂时不可用: {scan['error']}]\\n"
                f"[请如实告知用户数据获取失败，建议：1. 检查股票代码是否正确 2. 稍后重试 "
                f"3. 可以尝试查看该股票所属板块的整体表现。禁止编造任何价格或评分数据。]"
            ), ""

        # 尝试获取实时行情（可能因 API 限流失败，有 K 线兜底）
        realtime_price = deep.get('price')
        realtime_pct = deep.get('change_pct')
        if realtime_pct is None:
            realtime_pct = 0.0

        # 若无实时行情，用 K 线最后收盘价兜底
        last_close = None
        if kline is not None and hasattr(kline, 'iloc') and len(kline) > 0:
            last_close = float(kline['close'].iloc[-1])
            if not realtime_price:
                realtime_price = last_close
                # 用最后两根 K 线估算涨跌
                if len(kline) >= 2:
                    prev_close = float(kline['close'].iloc[-2])
                    realtime_pct = round((last_close - prev_close) / prev_close * 100, 2)

        # ── 从 K 线计算量价指标（兜底方案）──
        vol_ratio_str = "?"
        turnover_str = "?"
        amp_str = "?"
        net_main_str = "?"
        intraday_str = "?"
        # 尝试获取实时换手率
        try:
            tr = fetcher.turnover_rate(code)
            if tr is not None:
                turnover_str = f"{tr:.1f}%"
        except Exception:
            pass
        if kline is not None and hasattr(kline, 'iloc') and len(kline) >= 20:
            latest = kline.iloc[-1]
            vol_latest = float(latest.get('volume', 0))
            vol_avg_20 = float(kline['volume'].tail(20).mean())
            if vol_avg_20 > 0:
                vol_ratio = vol_latest / vol_avg_20
                vol_ratio_str = f"{vol_ratio:.1f}"
            # 振幅 = 当日振幅 / 前收
            hi = float(latest.get('high', 0))
            lo = float(latest.get('low', 0))
            pre = float(kline['close'].iloc[-2]) if len(kline) >= 2 else float(latest.get('open', 0))
            if pre > 0:
                amp = (hi - lo) / pre * 100
                amp_str = f"{amp:.1f}%"
            # 日内位置 = (收盘-最低)/(最高-最低)
            if hi > lo:
                intra = (last_close - lo) / (hi - lo) if last_close else 0
                intraday_str = f"{intra:.2f} ({'高位' if intra > 0.7 else '中位' if intra > 0.3 else '低位'})"
        # 标注数据来源
        data_source_note = "(基于K线计算)" if not deep.get('price') else "(实时行情)"

        # ── 信号详情（含等级和描述，quick_scan 已算好）──
        signal_lines = []
        raw_signals = deep.get('signals', scan.get('all_signals', []))
        for s in raw_signals[:6]:
            level = s.get('level', 1)
            stars = '★' * min(level, 5) + '☆' * max(0, 5 - min(level, 5))
            desc = s.get('desc', '')
            signal_lines.append(f"  [{stars}] {s['name']}{' — ' + desc if desc else ''}")

        # ── 量化因子详情（analyzer 已算好19项因子）──
        factor_lines = []
        fd = deep.get('factor_details', scan.get('factor_details', {}))
        if fd:
            top_factors = sorted(fd.items(), key=lambda x: abs(x[1]), reverse=True)[:8]
            for k, v in top_factors:
                bar = '█' * min(10, int(abs(v) / 3)) if abs(v) > 0 else ''
                direction = '↑' if v > 0 else ('↓' if v < 0 else '→')
                factor_lines.append(f"  {k}: {v:+.2f} {direction} {bar}")

        # ── 构建结构化上下文 ──
        kline_n = kline_nrows
        has_realtime = deep.get('price') is not None

        parts = [
            f"=== {code} 实时诊断 ===",
            f"数据源: {'实时行情' if has_realtime else 'K线计算'} + K线({kline_n}日) + 19项因子 + 技术/情绪信号扫描",
            f"数据标注: 量比/振幅/日内位置基于最近K线计算，主力净流入/换手率需实时API（暂不可用则标注?）",
            "",
            "▸ 量价数据",
            f"现价 {realtime_price or '?'}  涨跌 {realtime_pct:+.1f}%  "
            f"量比 {vol_ratio_str}  振幅 {amp_str}  日内位置 {intraday_str}",
            f"主力净流入 {net_main_str}  换手率 {turnover_str}  {data_source_note}",
            "",
            "▸ 技术研判",
            f"MA5 {deep.get('ma5', 0):.2f}  MA10 {deep.get('ma10', 0):.2f}  MA20 {deep.get('ma20', 0):.2f}",
            f"支撑 {deep.get('support', '?')}  阻力 {deep.get('resistance', '?')}",
            f"趋势定性: {deep.get('summary', '')}",
            "",
            "▸ 触发信号",
        ]
        parts.extend(signal_lines if signal_lines else ["  (无触发信号)"])
        parts.extend([
            "",
            "▸ 量化因子 (19项中Top8)",
        ])
        parts.extend(factor_lines if factor_lines else ["  (因子暂不可用)"])
        parts.extend([
            "",
            "▸ 概念属性",
            f"{', '.join(deep.get('concepts', [])) or '未识别'}",
        ])

        # ── 注入情绪面和消息面数据 ──
        store = get_signal_store()
        signals = store.get_all()
        sent_data = signals.get("sentiment", {}).get("data", {})
        market_sent = sent_data.get("market", {})
        if market_sent:
            parts.extend([
                "",
                "▸ 市场情绪",
                f"情绪指数: {market_sent.get('sentiment_index', '?')}  "
                f"炸板率: {round(market_sent.get('po_ban_rate', 0) * 100)}%  "
                f"轮动速度: {market_sent.get('rotation_speed', '?')}",
            ])
        news_data = signals.get("news", {}).get("data", {})
        news_events = news_data.get("events", []) if news_data else []
        if news_events:
            parts.extend([
                "",
                "▸ 相关消息",
            ])
            for ev in news_events[:5]:
                parts.append(f"  [{ev.get('sentiment','neutral')}] {ev.get('title','')}")

        # ── 日级别评分 ──
        ds = _get_daily_score_info(code)
        if ds:
            parts.extend([
                "",
                "▸ 日级别评分（盘后全市场扫描）",
                f"日综合{ds['combined_score']} | 技术{ds['signal_score']} + 情绪{ds['sentiment_score']} + 因子{ds['factor_score']}",
                f"全市场排名: #{ds['rank']}/{ds['total']} (前{ds['rank_pct']}%) | 信号: {ds['signal_names']}",
            ])

        parts.extend([
            "",
            "▸ 综合评分",
            f"盘中技术{deep.get('tech_score',0)} + 情绪{deep.get('sentiment_score',0)} + 因子{deep.get('factor_score',0)} = 综合{deep.get('combined_score',0)}",
            f"信号强度 {deep.get('max_signal_level',0)}级 / {deep.get('signal_count',0)}个",
            "",
            "上方K线表格是本诊断的核心数据源——近期量价分析必须:",
            "1. 引用具体日期的量价数据和标注事件",
            "2. 逐段解释关键转折（底部区域→放量启动→洗盘→反包→加速→天量的演变逻辑）",
            "3. 从量价历史中提取操作线索",
            "输出结构: 量价数据→近期量价分析→技术研判→资金面→综合评分→操作建议→一句话。",
            "每项分析必须有具体数字支撑。禁止说「数据不足」「无法获取」。禁止使用任何emoji。",
        ])
        ctx = '\n'.join(parts)
        return ctx, kline_csv
    except Exception as e:
        fallback = _build_daily_fallback_context(code)
        if fallback:
            logger.info("StockContext: 数据异常，日评分兜底 %s", code)
            return fallback, ""
        logger.warning("个股数据获取失败 %s: %s", code, e)
        return (
            f"[个股数据获取异常: {e}]\\n"
            f"[请告知用户当前无法获取实时数据，建议稍后重试或查看候选池中其他标的。"
            f"禁止编造数据。]"
        ), ""


@app.route("/api/stock/analyze")
def api_stock_analyze():
    """获取单只股票的实时分析数据"""
    code = request.args.get("code", "").strip()
    if not code or not re.match(r'^\d{6}$', code):
        return jsonify({"error": "请提供6位股票代码"}), 400

    try:
        from layer1_data.fetcher import DataFetcher
        from layer2_scan.screener import StockScreener
        from layer4_analysis.analyzer import StockAnalyzer
        fetcher = DataFetcher()
        screener = StockScreener(fetcher=fetcher)
        analyzer = StockAnalyzer(fetcher=fetcher)

        scan = screener.quick_scan(code)
        shared_kline = scan.get("kline") if "error" not in scan else None
        deep = analyzer.analyze_stock(code, kline=shared_kline)

        return jsonify({
            "stock_code": code,
            "price": deep.get("price"),
            "change_pct": deep.get("change_pct"),
            "support": deep.get("support"),
            "resistance": deep.get("resistance"),
            "concepts": deep.get("concepts", []),
            "signal_score": deep.get("combined_score"),
            "tech_score": deep.get("tech_score"),
            "sentiment_score": deep.get("sentiment_score"),
            "factor_score": deep.get("factor_score"),
            "signal_names": deep.get("signal_names", []),
            "summary": deep.get("summary", ""),
            "ma5": deep.get("ma5"),
            "ma10": deep.get("ma10"),
            "ma20": deep.get("ma20"),
        })
    except Exception as e:
        logger.error("个股分析失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 个股诊断卡片端点 ──────────────────────────────────────

@app.route("/api/stock/diagnose")
def api_stock_diagnose():
    """个股诊断 - 返回结构化 JSON 供前端卡片渲染。
    整合 K 线、信号、因子、情绪面、消息面、换手率。
    """
    code = request.args.get("code", "").strip()
    if not code or not re.match(r'^\d{6}$', code):
        return jsonify({"error": "请提供6位股票代码"}), 400

    try:
        from layer1_data.fetcher import DataFetcher
        from layer2_scan.screener import StockScreener
        from layer4_analysis.analyzer import StockAnalyzer
        fetcher = DataFetcher()
        screener = StockScreener(fetcher=fetcher)
        analyzer = StockAnalyzer(fetcher=fetcher)

        scan = screener.quick_scan(code)
        if "error" in scan:
            return jsonify({"error": scan["error"]}), 500
        shared_kline = scan.get("kline")
        deep = analyzer.analyze_stock(code, kline=shared_kline)

        # 换手率
        turnover = None
        try:
            turnover = fetcher.turnover_rate(code)
        except Exception:
            pass

        # K 线统计
        kline = shared_kline
        kline_stats = {}
        kline_table = ""
        kline_events_list = []  # 供前端高亮的事件列表
        if kline is not None and len(kline) >= 5:
            # 事件标注（与 _extract_stock_context 同源）
            annotations, evt_stats = _analyze_kline_events(kline)
            low_20 = evt_stats.get('low_20', round(float(kline['low'].tail(20).min()), 2))
            high_20 = evt_stats.get('high_20', round(float(kline['high'].tail(20).max()), 2))
            avg_amount = evt_stats.get('avg_amount',
                round(float((kline['volume'].tail(20) * kline['close'].tail(20) / 1e8).mean()), 1))
            pct_from_low = evt_stats.get('pct_from_low',
                round(float((kline['close'].iloc[-1] - low_20) / low_20 * 100), 1))
            kline_stats = {"low_20": low_20, "high_20": high_20,
                           "avg_amount": avg_amount, "pct_from_low": pct_from_low}

            # 生成 K 线文本表格（含事件标注）
            recent = kline.tail(18)
            kline_n = len(kline)
            lines = ["  日期     开盘   收盘    涨幅%     成交额"]
            lines.append("─" * 55)
            for row_idx, (_, row) in enumerate(recent.iterrows()):
                td = str(row.get('trade_date', ''))[:10]
                if '-' in td:
                    parts = td.split('-')
                    date_str = f"{parts[1]}/{parts[2]}"
                else:
                    date_str = td[-5:] if len(td) >= 5 else td
                o = float(row.get('open', 0))
                c = float(row.get('close', 0))
                chg = ((c - o) / o * 100) if o > 0 else 0
                amount = float(row.get('volume', 0)) * c / 1e8

                # 事件标注
                global_idx = kline_n - len(recent) + row_idx
                tags = annotations.get(global_idx, [])
                tag_str = ('  ← ' + ' '.join(tags)) if tags else ''
                if tags:
                    kline_events_list.append({"date": date_str, "tags": tags})

                lines.append(
                    f"  {date_str}  {o:>7.2f} {c:>7.2f}  "
                    f"{chg:>+6.1f}%  {amount:>5.0f}亿{tag_str}"
                )
            lines.append("─" * 55)
            lines.append(f"  20日最低 {low_20}  20日最高 {high_20}  "
                         f"20日均额 {avg_amount}亿  距底 {pct_from_low}%")
            kline_table = '\n'.join(lines)

        # 量价指标
        vol_ratio = None
        amp_val = None
        intraday = None
        if kline is not None and len(kline) >= 20:
            latest = kline.iloc[-1]
            vol_latest = float(latest.get('volume', 0))
            vol_avg_20 = float(kline['volume'].tail(20).mean())
            vol_ratio = round(vol_latest / vol_avg_20, 1) if vol_avg_20 > 0 else None
            hi = float(latest.get('high', 0))
            lo = float(latest.get('low', 0))
            pre = float(kline['close'].iloc[-2]) if len(kline) >= 2 else float(latest.get('open', 0))
            amp_val = round((hi - lo) / pre * 100, 1) if pre > 0 else None
            close_v = float(latest.get('close', 0))
            intraday = round((close_v - lo) / (hi - lo), 2) if hi > lo else None

        # 情绪面
        store = get_signal_store()
        signals_all = store.get_all()
        sentiment = signals_all.get("sentiment", {}).get("data", {}).get("market", {})

        # 消息面
        news_data = signals_all.get("news", {}).get("data", {})
        news_events = news_data.get("events", [])[:3] if news_data else []

        # 候选池排名
        data = select_stocks(collector=collector)
        pool = None
        for p in data.get("pool_a", []) + data.get("pool_b", []):
            if p.get("code") == code:
                pool = "A池" if p in data.get("pool_a", []) else "B池"
                break

        # 获取股票简称
        try:
            stock_name = ""
            mk = fetcher.current_market([code])
            if mk is not None and not mk.empty:
                stock_name = str(mk.iloc[0].get("short_name", ""))
        except Exception:
            stock_name = ""

        return jsonify({
            "code": code,
            "name": stock_name,
            "price": deep.get("price"),
            "change_pct": deep.get("change_pct"),
            "volume_ratio": vol_ratio,
            "turnover_rate": turnover,
            "amp": amp_val,
            "intraday": intraday,
            "net_main": None,
            "kline_table": kline_table,
            "kline_stats": kline_stats,
            "kline_events": kline_events_list,
            "signals": [{"level": s.get("level", 1), "name": s.get("name", ""),
                         "desc": s.get("desc", "")}
                        for s in deep.get("signals", scan.get("all_signals", []))],
            "factors": {k: round(v, 1) for k, v in deep.get("factor_details", {}).items()},
            "mas": {"ma5": round(deep.get("ma5", 0), 2),
                    "ma10": round(deep.get("ma10", 0), 2),
                    "ma20": round(deep.get("ma20", 0), 2)},
            "support": deep.get("support"),
            "resistance": deep.get("resistance"),
            "concepts": deep.get("concepts", [])[:5],
            "sentiment": {"index": sentiment.get("sentiment_index"),
                          "po_ban_rate": sentiment.get("po_ban_rate"),
                          "rotation": sentiment.get("rotation_speed"),
                          "lianban_rate": sentiment.get("lianban_rate")},
            "news": [{"sentiment": e.get("sentiment", "neutral"), "title": e.get("title", "")}
                     for e in news_events],
            "combined_score": deep.get("combined_score"),
            "signal_names": deep.get("signal_names", []),
            "summary": deep.get("summary", ""),
            "pool": pool,
            "daily_score": _get_daily_score_info(code),
        })
    except Exception as e:
        logger.error("个股诊断失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 板块诊断端点 ──────────────────────────────────────────────

@app.route("/api/sector/diagnose")
def api_sector_diagnose():
    """板块诊断 - 返回板块资金流时序 + 排名数据供卡片渲染。"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "请指定板块名称"}), 400

    # 查找板块时序数据
    ts_data = collector.get_sector_timeseries(name, recent_minutes=60) if collector else {}

    # 当前排名
    rank = None
    total = 0
    dash = collector.get_dashboard_data(sector_type="watch") if collector else {}
    rank_list = dash.get("rank", [])
    total = len(rank_list)
    for i, r in enumerate(rank_list):
        if r.get("name") == name:
            rank = i + 1
            break

    # 从 collector 获取板块成分股 top 资金流入
    constituents = []
    try:
        const = collector.get_sector_constituents(name) if hasattr(collector, 'get_sector_constituents') else []
        constituents = [{"code": c.get("code",""), "name": c.get("name",""),
                        "pct": c.get("pct_chg",0), "net_main": c.get("net_main",0)}
                       for c in const[:8]]
    except Exception:
        pass

    return jsonify({
        "name": name,
        "timeseries": ts_data,
        "rank": rank,
        "total": total,
        "constituents": constituents,
    })


# ── 持仓陪跑端点 ──────────────────────────────────────────────

@app.route("/api/holdings/analyze", methods=["POST"])
def api_holdings_analyze():
    """持仓陪跑分析 — 用户输入持仓信息，Agent 分析操作策略。"""
    body = request.get_json(silent=True) or {}
    positions = body.get("positions", [])
    if not positions:
        return jsonify({"error": "请提供持仓信息"}), 400

    # 为每只持仓获取实时数据
    enriched = []
    try:
        from layer1_data.fetcher import DataFetcher
        from layer2_scan.screener import StockScreener
        from layer4_analysis.analyzer import StockAnalyzer
        fetcher = DataFetcher()
        screener = StockScreener(fetcher=fetcher)
        analyzer = StockAnalyzer(fetcher=fetcher)

        for pos in positions:
            code = pos.get("code", "").strip()
            if not code or not re.match(r'^\d{6}$', code):
                continue
            entry = {
                "code": code,
                "cost": float(pos.get("cost", 0)),
                "shares": int(pos.get("shares", 0)),
                "weight": float(pos.get("weight", 0)),
            }
            # 获取实时数据
            try:
                scan = screener.quick_scan(code)
                if "error" not in scan:
                    shared_kline = scan.get("kline")
                    deep = analyzer.analyze_stock(code, kline=shared_kline)
                    mk = fetcher.current_market([code])
                    name = str(mk.iloc[0].get("short_name", "")) if mk is not None and not mk.empty else ""
                    entry["name"] = name
                    entry["price"] = deep.get("price")
                    entry["change_pct"] = deep.get("change_pct")
                    entry["signals"] = [s.get("name") for s in deep.get("signals", [])[:3]]
                    entry["combined_score"] = deep.get("combined_score")
                    entry["support"] = deep.get("support")
                    entry["resistance"] = deep.get("resistance")
                    entry["summary"] = deep.get("summary", "")
                    # 盈亏计算
                    if entry["price"] and entry["cost"]:
                        entry["pnl_pct"] = round((entry["price"] - entry["cost"]) / entry["cost"] * 100, 1)
                        entry["pnl_amount"] = round((entry["price"] - entry["cost"]) * entry["shares"], 0)
            except Exception as e:
                entry["error"] = str(e)
            enriched.append(entry)
    except Exception as e:
        logger.error("持仓分析数据获取失败: %s", e)

    # Agent 分析
    store = get_signal_store()
    signals = store.get_all()
    data = select_stocks(collector=collector)
    hot_sectors = data.get("hot_sectors", [])

    agent = get_agent()
    context = json.dumps({
        "positions": enriched,
        "market_breadth": data.get("market_breadth", 0.5),
        "risk_level": data.get("risk_level", "low"),
        "hot_sectors": hot_sectors[:5],
        "sentiment": signals.get("sentiment", {}).get("data", {}).get("market", {}),
    }, ensure_ascii=False, indent=2)

    user_msg = (
        f"我有以下持仓，请逐一分析操作策略：\n\n{context}\n\n"
        "对每只持仓输出：持仓诊断→操作建议（持有/加仓/减仓/清仓）→目标价→止损价→仓位调整建议。"
        "操作建议必须使用严格格式：操作: 持有/加仓/减仓/清仓  目标: X.XX  止损: X.XX  仓位调整: 文字说明"
    )

    reply = agent.chat(
        user_msg, [], hot_sectors, signals, [],
        stock_context="",
    )

    return jsonify({
        "positions": enriched,
        "analysis": reply or "Agent 暂时不可用",
        "market_context": {
            "breadth": data.get("market_breadth", 0.5),
            "risk_level": data.get("risk_level", "low"),
            "hot_sectors": hot_sectors[:5],
        },
    })


# ── Multi-Agent 辩论端点 ───────────────────────────────

@app.route("/api/agent/debate")
def api_agent_debate():
    """Multi-Agent 辩论：三位分析师 + 主席综合判断。"""
    data = select_stocks(collector=collector)
    all_candidates = data.get("pool_a", []) + data.get("pool_b", [])
    if not all_candidates:
        return jsonify({"error": "候选池为空"})

    store = get_signal_store()
    signals = store.get_all()
    breadth = data.get("market_breadth", 0.5)
    hot_sectors = data.get("hot_sectors", [])

    do = get_orchestrator()
    report = do.run_debate(all_candidates, signals, hot_sectors, breadth)
    if report:
        return jsonify(report)
    return jsonify({"error": "辩论系统不可用", "hint": "请查看 AI 观澜标签页"})

# ── Loop Engineering 端点 ───────────────────────────────

@app.route("/api/loop/adopt", methods=["POST"])
def api_loop_adopt():
    """用户采纳 AI 推荐，记录决策。"""
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"error": "股票代码不能为空"}), 400
    ds = get_decision_store()
    did = ds.record_decision(
        stock_code=code,
        stock_name=body.get("name", ""),
        entry_price=float(body.get("entry_price", 0)),
        sector=body.get("sector", ""),
        pool=body.get("pool", ""),
        signal=body.get("signal", ""),
        confidence=int(body.get("confidence", 3)),
        score=float(body.get("score", 0)),
        source=body.get("source", "agent"),
        notes=body.get("notes", ""),
    )
    return jsonify({"id": did, "status": "adopted"})


@app.route("/api/loop/status")
def api_loop_status():
    """获取决策闭环状态。"""
    ds = get_decision_store()
    la = get_loop_analyzer()
    return jsonify({
        "stats": ds.get_total_stats(),
        "signal_win_rates": ds.get_signal_win_rates(),
        "open_decisions": ds.get_open_decisions()[:10],
        "signal_hotness": la.get_signal_hotness()[:10],
        "context": ds.get_loop_context(),
    })


@app.route("/api/loop/exit", methods=["POST"])
def api_loop_exit():
    """手动标记退出。"""
    body = request.get_json(silent=True) or {}
    ds = get_decision_store()
    ds.mark_exited(
        decision_id=int(body.get("id", 0)),
        exit_price=float(body.get("exit_price", 0)),
    )
    return jsonify({"status": "exited"})


@app.route("/api/loop/auto-resolve", methods=["POST"])
def api_loop_auto_resolve():
    """手动触发自动结算。"""
    ds = get_decision_store()
    n = ds.auto_resolve()
    return jsonify({"resolved": n})

def _auto_run_daily_scorer():
    """交易日上午 9:25 后启动时，自动执行日评分扫描。"""
    now = datetime.now()
    # 只在交易日的 9:25-9:35 之间自动触发
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
