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
import signal
import sys
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path 中（用于导入 layer1_data 等模块）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, send_from_directory

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

# System B: 情绪面采集器（独立线程）
sentiment_collector = SentimentCollector()
# System C: 消息面监控（独立线程）
news_monitor = NewsMonitor()
# v4.0: 盘前预扫描
pre_market_scanner = PreMarketScanner()

# 启动信号定期持久化（每 60s）
get_signal_store().start_auto_persist(interval=60)

# 非 mock 模式启动 System B/C
if not _use_mock:
    sentiment_collector.start()
    logger.info("System B（情绪面）已启动")
    news_monitor.start()
    logger.info("System C（消息面）已启动")


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

    if collector is not None:
        data = collector.get_dashboard_data(sector_type=sector_type)
    else:
        data = fetch_dashboard_data(use_real=False)
        data["sector_type"] = sector_type
        data["data_date"] = data.get("date", "")
        # mock 模式下 watch 类型需要白名单过滤
        if sector_type == "watch":
            data["rank"] = [r for r in data.get("rank", []) if r["name"] in WATCH_SECTORS]
            data["series"] = {
                k: v for k, v in data.get("series", {}).items() if k in WATCH_SECTORS
            }
    return jsonify(data)


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
    if collector is not None:
        status = collector.get_status()
        status["mode"] = "live"
    else:
        try:
            from data_fetcher import is_trading_day
            trading_day = is_trading_day()
        except Exception:
            trading_day = datetime.now().weekday() < 5
        status = {
            "running": False,
            "snapshots_concept": 0,
            "snapshots_industry": 0,
            "snapshots_northbound": 0,
            "last_poll_iso": None,
            "consecutive_failures": 0,
            "poll_interval": 0,
            "market_open": _is_market_open_now(),
            "is_trading_day": trading_day,
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


# ── 盘中选股 + 多系统聚合 ──────────────────────────────────

@app.route("/api/stocks")
def api_stocks():
    """返回盘中选股候选池（多系统聚合）。

    System A（技术+资金）提供基础分，System B（情绪）调节策略基调，
    System C（消息）提供事件加分。降级：任何系统挂了自动分权。
    """
    use_mock = _use_mock
    if request.args.get("mock", "0") == "1":
        use_mock = True
    data = select_stocks(use_mock=use_mock, collector=collector)

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
    if pre_market.get("pre_market_open") and not _use_mock:
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
    use_mock = _use_mock
    if request.args.get("mock", "0") == "1":
        use_mock = True
    data = select_stocks(use_mock=use_mock, collector=collector)
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

    use_mock = _use_mock
    data = select_stocks(use_mock=use_mock, collector=collector)
    all_candidates = data.get("pool_a", []) + data.get("pool_b", [])
    store = get_signal_store()
    signals = store.get_all()
    hot_sectors = data.get("hot_sectors", [])

    agent = get_agent()
    # 检测用户消息中的股票代码并获取实时数据
    stock_data_context, kline_data = _extract_stock_context(user_message)
    reply = agent.chat(
        user_message, all_candidates, hot_sectors, signals, chat_history,
        stock_context=stock_data_context,
    )
    if reply:
        return jsonify({"reply": reply, "kline": kline_data})
    return jsonify({"error": "Agent 不可用", "reply": "抱歉，AI 助手暂时不可用，请稍后重试。"})


# ── 个股分析端点 ──────────────────────────────────────────────

def _extract_stock_code(text):
    """从文本中提取6位股票代码"""
    m = re.search(r'\b(\d{6})\b', text)
    return m.group(1) if m else None

def _extract_stock_context(user_message):
    """返回 (LLM上下文, K线表格数据) 的元组"""
    code = _extract_stock_code(user_message)
    if not code:
        return "", ""
    try:
        from layer1_data.fetcher import DataFetcher
        from layer2_scan.screener import StockScreener
        from layer4_analysis.analyzer import StockAnalyzer
        fetcher = DataFetcher()
        screener = StockScreener(fetcher=fetcher)
        analyzer = StockAnalyzer(fetcher=fetcher)

        scan = screener.quick_scan(code)
        deep = analyzer.analyze_stock(code)

        # K线表格（纯文本，前端渲染为HTML表格）
        kline_csv = ""
        kline = scan.get("kline") if "error" not in scan else None
        if kline is not None and hasattr(kline, 'tail'):
            recent = kline.tail(15)
            kline_csv = "日期 开盘 收盘 涨幅 成交量(亿)\n"
            for idx, row in recent.iterrows():
                date_str = str(idx)[:10]
                o = float(row.get('open', 0))
                c = float(row.get('close', 0))
                chg = ((c - o) / o * 100) if o > 0 else 0
                vol = float(row.get('volume', 0)) / 1e8
                kline_csv += f"{date_str} {o:.2f} {c:.2f} {chg:+.1f}% {vol:.1f}\n"

        if "error" in scan:
            return f"[用户询问股票 {code}，数据获取失败: {scan['error']}]", ""

        ctx = f"""
以下为脚本获取的 {code} 实时K线数据，请据此诊断。

现价 {deep.get('price', '?')}  涨跌 {deep.get('change_pct', 0):+.1f}%
MA5 {deep.get('ma5', 0):.2f}  MA10 {deep.get('ma10', 0):.2f}  MA20 {deep.get('ma20', 0):.2f}
支撑 {deep.get('support', '?')}  阻力 {deep.get('resistance', '?')}
概念: {', '.join(deep.get('concepts', []))}
评分 {deep.get('combined_score', 0)}  信号: {', '.join(deep.get('signal_names', []))}

K线数据已在UI表格中展示，你不需要重复列出。请直接输出: 综合研判→操作建议→对比表格。"""
        return ctx, kline_csv
    except Exception as e:
        logger.warning("个股数据获取失败 %s: %s", code, e)
        return "", ""


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
        deep = analyzer.analyze_stock(code)

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



# ── Multi-Agent 辩论端点 ───────────────────────────────

@app.route("/api/agent/debate")
def api_agent_debate():
    """Multi-Agent 辩论：三位分析师 + 主席综合判断。"""
    use_mock = _use_mock
    if request.args.get("mock", "0") == "1":
        use_mock = True
    data = select_stocks(use_mock=use_mock, collector=collector)
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
