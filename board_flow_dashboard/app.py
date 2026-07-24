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
    from .report_store import get_report_store, check_and_generate, REPORT_META, REPORT_TYPES
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
    from report_store import get_report_store, check_and_generate, REPORT_META, REPORT_TYPES  # type: ignore[no-redef]

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
collector = SectorFlowCollector(poll_interval=180.0)
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

# 启动选股预热线程（交易时段每 50s 自动刷新缓存）
try:
    from .stock_selector import start_stock_warmup
except ImportError:
    from stock_selector import start_stock_warmup  # type: ignore[no-redef]
start_stock_warmup()
logger.info("选股预热线程已启动")

# 持仓盯盘助手（交易时段轮询持仓价格，触及点位主动推送）
try:
    from .position_watcher import get_position_watcher
except ImportError:
    from position_watcher import get_position_watcher  # type: ignore[no-redef]
position_watcher = get_position_watcher()
position_watcher.start()
logger.info("持仓盯盘助手已启动")

# P1-1: 决策自动结算调度（让胜率飞轮自动运转，不依赖手动点按钮）
try:
    get_decision_store().start_auto_resolve(check_interval=3600.0)
except Exception as _e:
    logger.warning("自动结算调度启动失败: %s", _e)


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
    # 停止选股预热
    try:
        from .stock_selector import stop_stock_warmup
    except ImportError:
        from stock_selector import stop_stock_warmup  # type: ignore[no-redef]
    stop_stock_warmup()
    # 停止持仓盯盘
    try:
        position_watcher.stop()
    except Exception:
        pass
    _save_market_daily_cache()

atexit.register(_cleanup)

def _signal_handler(signum, frame):
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ── 板块资金流向 ────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    """获取板块看板数据。?type=concept（默认）或 industry&sectors=a,b,c"""
    sector_type = request.args.get("type", "watch")
    if sector_type not in ("concept", "industry", "watch"):
        sector_type = "watch"
    date_param = request.args.get("date", "").strip()
    sectors_param = request.args.get("sectors", "").strip()

    if date_param:
        data = collector.get_dashboard_data_for_date(date_param, sector_type=sector_type)
    else:
        data = collector.get_dashboard_data(sector_type=sector_type,
                                             watch_sectors=sectors_param.split(",") if sectors_param else None)
    return jsonify(data)


@app.route("/api/data/delta")
def api_data_delta():
    """增量更新：仅返回 since_time_idx 之后的新数据点。

    前端轮询时使用，避免每次传输全量 ~30KB JSON。
    ?type=watch&since=N&sectors=a,b,c
    """
    sector_type = request.args.get("type", "watch")
    if sector_type not in ("concept", "industry", "watch"):
        sector_type = "watch"
    since = int(request.args.get("since", -1))
    sectors_param = request.args.get("sectors", "").strip()

    if collector is not None:
        data = collector.get_dashboard_delta(
            since,
            watch_sectors=sectors_param.split(",") if sectors_param else None,
        )
    else:
        data = {"time_label": "--:--", "time_index": 0, "total_times": 0,
                "new_data": {}, "rank": [], "is_trading": False}
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
    data = collector.get_sector_timeseries(name, recent_minutes=minutes)
    return jsonify(data)


@app.route("/api/sectors/available")
def api_sectors_available():
    """返回当前 API 快照中所有可用的板块名称（供自选板块增删搜索）。

    从概念+行业快照中提取去重板块名，排除财报分类等非板块条目。
    """
    if not collector:
        return jsonify({"sectors": []})
    with collector._lock:
        concept = list(collector._concept_snapshots)
        industry = list(collector._industry_snapshots)
    names = set()
    # 过滤财报/日期分类：排除以年份开头的条目
    import re as _re
    report_pattern = _re.compile(r'^\d{4}')
    for snap in concept + industry:
        for r in snap.get("rank", []):
            n = r.get("name", "")
            if n and not report_pattern.match(n):
                names.add(n)
    return jsonify({"sectors": sorted(names)})


# ── 大盘数据 ──────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"

# 日级缓存（成交额对照）
_market_daily_cache: dict = {}
_market_cache: dict = {}

def _load_market_daily_cache():
    global _market_daily_cache
    try:
        cf = DATA_DIR / "market_daily_cache.json"
        if cf.exists():
            _market_daily_cache = json.loads(cf.read_text())
        # 如果是新的一天，把昨天的值挪到 _prev 供对比
        today = datetime.now().strftime("%Y%m%d")
        if _market_daily_cache.get("_date") != today:
            yesterday_amt = _market_daily_cache.get("total_amount", 0)
            _market_daily_cache = {"_date": today, "_prev_amount": yesterday_amt, "_prev_date": _market_daily_cache.get("_date", "")}
    except Exception:
        _market_daily_cache = {"_date": datetime.now().strftime("%Y%m%d"), "_prev_amount": 0, "_prev_date": ""}

def _save_market_daily_cache():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "market_daily_cache.json").write_text(json.dumps(_market_daily_cache))
    except Exception:
        pass

_load_market_daily_cache()

# 日评分缓存（降级估算涨跌家数用）
_DAILY_SCORES_CACHE: dict = {}

def _load_daily_scores_for_breadth():
    global _DAILY_SCORES_CACHE
    if _DAILY_SCORES_CACHE:
        return
    try:
        today = datetime.now().strftime("%Y%m%d")
        cf = DATA_DIR / f"daily_scores_{today}.json"
        if cf.exists():
            _DAILY_SCORES_CACHE = json.loads(cf.read_text())
    except Exception:
        pass

# 全市场涨跌缓存（60秒刷新）
_adv_decline_cache: Optional[dict] = None
_adv_decline_ts: float = 0
_all_stock_codes_cache: list[str] = []

def _get_all_stock_codes() -> list[str]:
    """从 adata 本地缓存获取全市场 ~5500 只股票代码（含创业板/科创板/北交）。"""
    global _all_stock_codes_cache
    if _all_stock_codes_cache:
        return _all_stock_codes_cache
    try:
        cache_path = os.path.expanduser(
            "~/Library/Python/3.9/lib/python/site-packages/adata/stock/cache/code.csv"
        )
        if os.path.exists(cache_path):
            import pandas as pd
            df = pd.read_csv(cache_path)
            if "stock_code" in df.columns:
                codes = df["stock_code"].astype(str).str.zfill(6)
                # 过滤退市/B股
                if "short_name" in df.columns:
                    mask = ~df["short_name"].str.contains("退|B股", na=False)
                    codes = codes[mask]
                _all_stock_codes_cache = codes.tolist()
                logger.info("全市场股票代码: %d 只", len(_all_stock_codes_cache))
                return _all_stock_codes_cache
    except Exception as e:
        logger.warning("读取 adata 股票缓存失败: %s", e)
    return []

def _fetch_market_adv_decline() -> Optional[dict]:
    """获取全市场涨跌家数 + 涨停/跌停。

    主源: adata.stock.market.list_market_current (~5500只全覆盖)
    备源: 新浪总股数 + collector market_breadth 估算
    缓存 60 秒。
    """
    global _adv_decline_cache, _adv_decline_ts
    import time as _time
    now = _time.time()
    if _adv_decline_cache and (now - _adv_decline_ts) < 60:
        return _adv_decline_cache

    # ── 主源: adata 批量查询 ──
    codes = _get_all_stock_codes()
    if codes:
        try:
            import adata
            all_up, all_down = 0, 0
            all_lu, all_ld, total = 0, 0, 0
            for i in range(0, len(codes), 500):
                chunk = codes[i:i+500]
                try:
                    df = adata.stock.market.list_market_current(code_list=chunk)
                    if df is None or df.empty:
                        continue
                    pct = df['change_pct'].astype(float)
                    total += len(df)
                    all_up += (pct > 0).sum()
                    all_down += (pct < 0).sum()
                    all_lu += (pct >= 9.8).sum()
                    all_ld += (pct <= -9.8).sum()
                except Exception:
                    continue
            if total > 0:
                _adv_decline_cache = {
                    "up": int(all_up), "down": int(all_down),
                    "total": int(total), "limit_up": int(all_lu),
                    "limit_down": int(all_ld), "_src": "adata",
                }
                _adv_decline_ts = now
                return _adv_decline_cache
        except Exception as e:
            logger.warning("adata 全市场涨跌获取失败: %s", e)

    # ── 备源: 新浪总数 + collector breadth 估算 ──
    try:
        import requests as _rq3
        s = _rq3.Session(); s.trust_env = False
        r = s.get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a", timeout=5)
        total_sina = int(r.text.strip().strip('"'))
        if total_sina > 1000:
            # 从 DB 获取 sector-based breadth
            try:
                import sqlite3
                conn = sqlite3.connect(str(DATA_DIR / "collector.db"))
                today = datetime.now().strftime("%Y-%m-%d")
                row = conn.execute("SELECT data FROM concept_snapshots WHERE date=? ORDER BY id DESC LIMIT 1", (today,)).fetchone()
                conn.close()
                if row:
                    data = json.loads(row[0])
                    sectors = data.get("sectors", [])
                    up_s = sum(1 for s in sectors if (s.get("pct_chg") or 0) > 0)
                    b = up_s / len(sectors) if sectors else 0.5
                else:
                    b = 0.5
            except Exception:
                b = 0.5
            up_est = int(total_sina * b)
            _adv_decline_cache = {
                "up": up_est, "down": total_sina - up_est,
                "total": total_sina, "limit_up": 0, "limit_down": 0,
                "_src": "sina_est",
            }
            _adv_decline_ts = now
            return _adv_decline_cache
    except Exception as e:
        logger.warning("新浪涨跌估算失败: %s", e)

    return None


@app.route("/api/market/overview")
def api_market_overview():
    """大盘概览：三大指数 + 6 个核心情绪指标，含降级标记。

    数据源优先级：sentiment_collector > collector.northbound > Tencent qt > 缓存
    """
    import time as _time
    result = {"indices": [], "metrics": {}, "_ts": datetime.now().strftime("%H:%M:%S")}

    # ── 三大指数 ── 直接用腾讯 API，指定正确前缀
    index_codes = {
        "上证指数": "sh000001",
        "深证成指": "sz399001",
        "创业板指": "sz399006",
    }
    import requests as _rq
    tencent_sess = _rq.Session()
    tencent_sess.trust_env = False
    df_rows = []
    try:
        url = "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006"
        resp = tencent_sess.get(url, timeout=5)
        resp.encoding = 'gbk'
        for line in resp.text.strip().split(';\n'):
            if '=' not in line: continue
            _, value = line.split('=', 1)
            value = value.strip().strip('"').strip("'")
            fields = value.split('~')
            if len(fields) < 33: continue
            # field[37] = 成交额(万元), 仅上证/深证指数有此字段
            amount = float(fields[37]) if len(fields) > 37 and fields[37] else 0
            df_rows.append({
                "stock_code": fields[2],
                "short_name": fields[1],
                "price": float(fields[3]) if fields[3] else 0,
                "change_pct": float(fields[32]) if fields[32] else 0,
                "change": float(fields[31]) if fields[31] else 0,
                "amount": amount,  # 万元
            })
    except Exception as e:
        logger.warning("腾讯指数行情失败: %s", e)

    for name, qt_code in index_codes.items():
        found = None
        for r in df_rows:
            if r.get("stock_code") == qt_code or qt_code.endswith(r.get("stock_code", "")):
                found = r
                break
        if found:
            result["indices"].append({
                "name": name,
                "price": round(found["price"], 2),
                "change_pct": round(found["change_pct"], 2),
                "change": round(found.get("change", 0), 2),
                "_src": "live",
            })
        else:
            cached = _market_cache.get(name)
            if cached:
                cached["_src"] = "cache"
                result["indices"].append(cached)
            else:
                result["indices"].append({"name": name, "price": None, "change_pct": None, "change": None, "_src": "none"})

    # 缓存本次成功数据
    for idx in result["indices"]:
        if idx.get("_src") == "live":
            _market_cache[idx["name"]] = {
                "name": idx["name"], "price": idx["price"],
                "change_pct": idx["change_pct"], "change": idx["change"],
            }

    # ── 6 个核心指标 ──
    store = get_signal_store()
    signals = store.get_all()
    sent_data = signals.get("sentiment", {}).get("data", {})
    market = sent_data.get("market", {}) if sent_data else {}

    # 涨跌家数 + 涨停/跌停
    adv_decline = _fetch_market_adv_decline()
    adv_src = adv_decline.get("_src", "live") if adv_decline else "none"
    if adv_decline:
        result["metrics"]["breadth"] = {"value": f"{adv_decline['up']}/{adv_decline['down']}", "label": "上涨/下跌", "_src": adv_src}
        lu = adv_decline.get("limit_up", 0)
        ld = adv_decline.get("limit_down", 0)
        if lu > 0 or ld > 0:
            result["metrics"]["limit"] = {"value": f"{lu}/{ld}", "label": "涨停/跌停", "_src": adv_src}
        else:
            result["metrics"]["limit"] = {"value": f"{lu}/{ld}", "label": "涨停/跌停", "_src": "fallback"}
    else:
        result["metrics"]["breadth"] = {"value": None, "label": "上涨/下跌", "_src": "none"}
        result["metrics"]["limit"] = {"value": None, "label": "涨停/跌停", "_src": "none"}

    # 缓存日评数据供降级使用
    _load_daily_scores_for_breadth()

    # 炸板率
    po_ban = market.get("po_ban_rate")
    if po_ban is not None:
        result["metrics"]["po_ban"] = {"value": str(round(po_ban * 100)) + "%", "label": "炸板率", "_src": "live"}
    else:
        result["metrics"]["po_ban"] = {"value": None, "label": "炸板率", "_src": "none"}

    # 情绪指数
    sent_idx = market.get("sentiment_index")
    if sent_idx is not None:
        level = "过热" if sent_idx > 80 else ("偏热" if sent_idx > 60 else ("中性" if sent_idx > 40 else ("偏冷" if sent_idx > 20 else "冰点")))
        result["metrics"]["sentiment"] = {"value": int(sent_idx), "label": f"情绪·{level}", "_src": "live"}
    else:
        result["metrics"]["sentiment"] = {"value": None, "label": "情绪指数", "_src": "none"}

    # 全市场成交额 — 主源腾讯，备源新浪
    total_amount = 0
    for r in df_rows:
        code = r.get("stock_code", "")
        if code in ("399006",):  # 创业板指是深证子集，跳过避免重复计算
            continue
        amt = r.get("amount", 0) or 0
        if amt > 0:
            total_amount += amt

    # 备源：新浪财经（腾讯失败时）
    if total_amount == 0:
        try:
            sina_sess = _rq.Session(); sina_sess.trust_env = False
            sr = sina_sess.get("https://hq.sinajs.cn/list=sh000001,sz399001", timeout=5,
                               headers={"Referer": "https://finance.sina.com.cn"})
            sr.encoding = "gbk"
            for line in sr.text.strip().split("\n"):
                if "=" not in line: continue
                _, v = line.split("=", 1); v = v.strip().strip('"')
                fields = v.split(",")
                if len(fields) > 9 and fields[9]:
                    total_amount += float(fields[9]) / 1e4  # 新浪是元→万元
            result["_amount_src"] = "sina"
        except Exception:
            pass
    if total_amount > 0:
        amt_yi = round(total_amount / 1e4)
        # 较上一日变化（绝对值）
        prev = _market_daily_cache.get("_prev_amount", 0)
        delta_str = ""
        if prev > 0:
            delta = amt_yi - prev
            sign = "+" if delta >= 0 else ""
            delta_str = f" ({sign}{delta})"
        _market_daily_cache["total_amount"] = amt_yi
        _save_market_daily_cache()
        result["metrics"]["total_amount"] = {"value": str(amt_yi) + delta_str, "label": "成交额(亿)", "_src": result.get("_amount_src", "live")}
    else:
        result["metrics"]["total_amount"] = {"value": None, "label": "成交额(亿)", "_src": "none"}

    return jsonify(result)


# 大盘数据 session 缓存
_market_cache: dict = {}

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


# ── 研究报告端点 ──────────────────────────────────────────

@app.route("/api/research/reports")
def api_research_reports():
    """返回指定日期的所有报告。?date=YYYY-MM-DD（可选，默认今天）。"""
    date_param = request.args.get("date", "").strip() or None
    store = get_report_store()
    reports = store.get_by_date(date_param)

    # 补齐未生成的报告（返回占位信息供前端渲染时间线）
    today = (date_param or datetime.now().strftime("%Y-%m-%d"))
    existing_types = {r["report_type"] for r in reports}

    result = []
    for rtype in ["pre_market", "morning_close", "midday_preview", "full_day_review"]:
        # 周末只显示已有的
        is_weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5 if len(today) == 10 else False
        meta = REPORT_META.get(rtype, {})

        if rtype in existing_types:
            r = next(r for r in reports if r["report_type"] == rtype)
            result.append({
                "id": r["id"],
                "type": r["report_type"],
                "title": r["title"],
                "content": r["content"],
                "data": json.loads(r["data_json"]) if r.get("data_json") else {},
                "date": r["report_date"],
                "generated_at": r["generated_at"],
                "status": r.get("status", "generated"),
            })
        elif not is_weekend and today == datetime.now().strftime("%Y-%m-%d"):
            # 今天还没到时间的报告
            now_hm = datetime.now().hour * 60 + datetime.now().minute
            due_hm = meta.get("hour", 0) * 60 + meta.get("minute", 0)
            result.append({
                "id": None,
                "type": rtype,
                "title": meta.get("title", rtype),
                "content": "",
                "data": {},
                "date": today,
                "generated_at": None,
                "status": "pending" if now_hm < due_hm else "generating",
                "due_time": meta.get("time", ""),
            })

    # 周五加周度总结
    if len(today) == 10:
        dt = datetime.strptime(today, "%Y-%m-%d")
        if dt.weekday() == 4:
            wk_in = [r for r in reports if r["report_type"] == "weekly_summary"]
            if wk_in:
                r = wk_in[0]
                result.append({
                    "id": r["id"], "type": r["report_type"], "title": r["title"],
                    "content": r["content"],
                    "data": json.loads(r["data_json"]) if r.get("data_json") else {},
                    "date": r["report_date"], "generated_at": r["generated_at"],
                    "status": r.get("status", "generated"),
                })
            elif today == datetime.now().strftime("%Y-%m-%d"):
                result.append({
                    "id": None, "type": "weekly_summary",
                    "title": "周度总结", "content": "", "data": {},
                    "date": today, "generated_at": None,
                    "status": "pending", "due_time": "周五 15:30",
                })

    return jsonify({"reports": result, "date": today})


@app.route("/api/research/report/<int:report_id>")
def api_research_report(report_id):
    """获取单个报告完整内容。"""
    store = get_report_store()
    r = store.get_by_id(report_id)
    if not r:
        return jsonify({"error": "报告不存在"}), 404
    return jsonify({
        "id": r["id"],
        "type": r["report_type"],
        "title": r["title"],
        "content": r["content"],
        "data": json.loads(r["data_json"]) if r.get("data_json") else {},
        "date": r["report_date"],
        "generated_at": r["generated_at"],
        "status": r.get("status", "generated"),
        "meta": REPORT_META.get(r["report_type"], {}),
    })


@app.route("/api/research/history")
def api_research_history():
    """返回有报告的日期列表。"""
    store = get_report_store()
    dates = store.get_available_dates(limit=60)
    return jsonify({"dates": dates})


@app.route("/api/research/generate", methods=["POST"])
def api_research_generate():
    """手动触发报告生成。?type=pre_market|morning_close|...&force=1"""
    rtype = request.args.get("type", "pre_market").strip()
    force = request.args.get("force", "0") == "1"

    if rtype not in REPORT_TYPES:
        return jsonify({"error": f"未知报告类型: {rtype}"}), 400

    store = get_report_store()
    gen_map = {
        "pre_market": store.generate_pre_market,
        "morning_close": store.generate_morning_close,
        "midday_preview": store.generate_midday_preview,
        "full_day_review": store.generate_full_day_review,
        "weekly_summary": store.generate_weekly_summary,
        "signal_review": store.generate_signal_review,
    }

    try:
        r = gen_map[rtype](force=force)
        if r:
            return jsonify({"status": "ok", "report": {
                "id": r["id"], "type": r["report_type"], "title": r["title"],
                "content": r["content"][:500], "date": r["report_date"],
                "generated_at": r["generated_at"],
            }})
        return jsonify({"status": "skipped", "message": f"{rtype} 不可生成（时间未到或已存在）"})
    except Exception as e:
        logger.error("报告生成失败 %s: %s", rtype, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/check")
def api_research_check():
    """检查并自动生成应生成的报告（前端轮询此端点）。"""
    generated = check_and_generate()
    return jsonify({"generated": generated, "time": datetime.now().strftime("%H:%M:%S")})


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
    # P0-3: 注入历史决策反馈上下文，让选股 Agent 参考历史胜率（闭环飞轮）
    loop_context = ""
    try:
        loop_context = get_decision_store().get_loop_context()
    except Exception as e:
        logger.debug("loop_context 获取失败(不阻断选股): %s", e)
    result = agent.generate_intraday_picks(
        all_candidates, hot_sectors, signals, breadth,
        loop_context=loop_context,
    )
    if result:
        result["mode"] = data.get("mode", "live")
        result["risk_level"] = data.get("risk_level", "low")
        # P1-5: 把 Agent 推荐写入影子模式（当日去重），供后续度量选择价值/超额收益
        try:
            picks = result.get("top_picks", []) or []
            cand_by_code = {c.get("code", ""): c for c in all_candidates}
            shadow = []
            for p in picks:
                c = cand_by_code.get(p.get("code", ""), {})
                shadow.append({
                    "code": p.get("code", ""),
                    "name": p.get("name", "") or c.get("name", ""),
                    "sector": c.get("sector", ""),
                    "pool": c.get("pool", ""),
                    "signal": c.get("signal", ""),
                    "price": c.get("price", 0) or 0,
                    "confidence": p.get("confidence", 3),
                    "score": c.get("score", 0) or 0,
                })
            if shadow:
                get_decision_store().record_shadow_batch_daily(shadow)
        except Exception as e:
            logger.debug("影子记录跳过(不阻断选股): %s", e)
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
    market_breadth = data.get("market_breadth", 0.5)  # P1-2: 真实市场广度

    agent = get_agent()
    # 检测用户消息中的股票（代码或名称）并获取实时数据
    resolved = _resolve_stock_code(user_message)
    resolved_code = resolved[0] if resolved else ""
    resolved_name = resolved[1] if resolved else ""
    stock_data_context, kline_data = _extract_stock_context(user_message, diagnose_data=diagnose_data)

    # 安全网：如果消息含股票但数据获取失败，直接返回错误，防止 LLM 编造数据
    stock_code_in_msg = resolved_code
    logger.info("Chat: code=%s, name=%s, ctx_len=%d, kline_len=%d, msg=%.80s",
                stock_code_in_msg, resolved_name, len(stock_data_context), len(kline_data), user_message)
    if stock_code_in_msg and not stock_data_context:
        label = f"{resolved_name}({stock_code_in_msg})" if resolved_name else stock_code_in_msg
        return jsonify({
            "reply": (
                f"⚠️ 无法获取 {label} 的实时数据。\n\n"
                f"可能原因：1. 数据接口超时 2. 股票代码错误 3. 非交易时段数据未更新\n\n"
                f"建议：稍后重试，或查看该股票所属板块的整体表现。"
            ),
            "kline": "",
        })

    # 检测用户消息中的板块名称并获取资金流时序数据
    sector_ts_context = ""
    for sector in WATCH_SECTORS:
        if sector in user_message:
            ts = collector.get_sector_timeseries(sector, recent_minutes=30)
            if "error" not in ts:
                sector_ts_context = json.dumps(ts, ensure_ascii=False)
            break

    reply = agent.chat(
        user_message, all_candidates, hot_sectors, signals, chat_history,
        stock_context=stock_data_context,
        sector_timeseries=sector_ts_context,
        resolved_code=resolved_code,
        breadth=market_breadth,
    )
    qt = agent._classify_question(user_message, resolved_code)
    _card_map = {"stock_analysis":"stock","sector_analysis":"sector","market_analysis":"market","external_info":"market","holding_decision":"holding"}
    card_type = _card_map.get(qt, "text")
    if card_type == "stock" and not stock_data_context:
        card_type = "text"
    if reply:
        return jsonify({
            "reply": reply,
            "kline": kline_data,
            "_ts": datetime.now().strftime("%H:%M:%S"),
            "_code": stock_code_in_msg or "",
            "_ctx": len(stock_data_context),
            "card_type": card_type,
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
    market_breadth = data.get("market_breadth", 0.5)  # P1-2: 真实市场广度

    agent = get_agent()
    # 检测用户消息中的股票（代码或名称）
    resolved = _resolve_stock_code(user_message)
    resolved_code = resolved[0] if resolved else ""
    resolved_name = resolved[1] if resolved else ""
    stock_data_context, kline_data = _extract_stock_context(user_message, diagnose_data=diagnose_data)

    stock_code_in_msg = resolved_code
    if stock_code_in_msg and not stock_data_context:
        label = f"{resolved_name}({stock_code_in_msg})" if resolved_name else stock_code_in_msg
        return jsonify({
            "reply": f"⚠️ 无法获取 {label} 的实时数据。请稍后重试。",
            "kline": "",
        })

    # 板块时序上下文
    sector_ts_context = ""
    for sector in WATCH_SECTORS:
        if sector in user_message:
            ts = collector.get_sector_timeseries(sector, recent_minutes=30)
            if "error" not in ts:
                sector_ts_context = json.dumps(ts, ensure_ascii=False)
            break

    def generate():
        ts = datetime.now().strftime("%H:%M:%S")
        ctx_len = len(stock_data_context)

        # 预分类问题类型 → card_type（前端据此选择卡片壳）
        qt = agent._classify_question(user_message, resolved_code)
        _card_map = {
            "stock_analysis": "stock", "sector_analysis": "sector",
            "market_analysis": "market", "external_info": "market",
            "holding_decision": "holding",
        }
        card_type = _card_map.get(qt, "text")
        # 个股诊断需要 diagData 才有完整卡片，否则降级为文本
        if card_type == "stock" and not stock_data_context:
            card_type = "text"

        # Phase 1: 推送元数据（含 card_type，前端立即渲染卡片壳）
        yield f"data: {json.dumps({'type': 'meta', 'card_type': card_type, 'kline': kline_data, '_ts': ts, '_code': stock_code_in_msg or '', '_name': resolved_name or '', '_ctx': ctx_len}, ensure_ascii=False)}\n\n"

        # Phase 2: 流式推送 LLM 输出
        full_reply = ""
        agent_error = False
        try:
            for chunk in agent.chat_stream(
                user_message, all_candidates, hot_sectors, signals,
                chat_history, stock_data_context, sector_ts_context,
                resolved_code=resolved_code,
                breadth=market_breadth,
            ):
                if chunk is None:
                    agent_error = True
                    break
                full_reply += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'data': chunk}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error("SSE stream error: %s", e)
            agent_error = True

        # Phase 3: 完成信号（含错误标记，前端可据此兜底）
        yield f"data: {json.dumps({'type': 'done', '_ts': ts, '_code': stock_code_in_msg or '', '_name': resolved_name or '', '_ctx': ctx_len, 'error': agent_error}, ensure_ascii=False)}\n\n"

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


# ── 股票名称→代码全局索引（惰性加载）─────────────────────────
_stock_name_index: Optional[dict] = None  # {"贵州茅台": "600519", "茅台": "600519", ...}


def _build_stock_name_index() -> dict:
    """构建股票名称→代码映射表。

    数据来源：
    1. adata 缓存的全部A股列表（~5000只，含完整名称）
    2. 候选池实时数据（名称别名/简称）

    每个股票产生多条索引：
    - 完整名称 → 代码
    - 去后缀简称（去掉"科技"/"股份"/"集团"等）→ 代码
    - 2-4字核心品牌名（仅当唯一时）→ 代码
    """
    global _stock_name_index
    if _stock_name_index is not None:
        return _stock_name_index

    idx: dict[str, str] = {}

    # 来源1: adata 缓存的全量股票列表
    try:
        from layer1_data.fetcher import DataFetcher
        fetcher = DataFetcher()
        df = fetcher.all_stocks()
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                code = str(row.get("stock_code", "")).strip()
                name = str(row.get("short_name", "")).strip()
                if not code or not name or len(code) != 6:
                    continue
                # 完整名称
                idx[name] = code
                # 去常见后缀
                short = re.sub(r'(科技|股份|集团|控股|实业|医药|电子|光电|智能|'
                               r'新材|材料|电气|装备|重工|精工|能源|环境|'
                               r'食品|饮料|传媒|通信|数据|软件|互联|激光|'
                               r'生物|医疗|检测|技术|建设|工程|服务|银行|'
                               r'证券|保险|信托|租赁|地产|物业|投资|发展|'
                               r'有限|有限公司|股份公司)$', '', name)
                if short and short != name and len(short) >= 2:
                    if short not in idx:
                        idx[short] = code
                # 2-4字核心品牌名（仅当唯一时）
                core = re.sub(r'[科技股份集团控股实业有限公司A-Za-z]', '', name)
                if 2 <= len(core) <= 4 and core != name:
                    if core not in idx:
                        idx[core] = code
                    else:
                        # 冲突：标记为 None 表示不唯一
                        idx[core] = None  # type: ignore
                # 尾部品牌名：A股名称常为"地域+品牌"结构，取后2-3字作为品牌简称
                # 如"贵州茅台"→"茅台"、"宁德时代"→"时代"
                if len(name) >= 4:
                    for k in [name[-2:], name[-3:]]:
                        if k not in idx:
                            idx[k] = code
                        elif idx[k] is not None and idx[k] != code:
                            idx[k] = None  # type: ignore
            logger.info("股票名称索引: %d 条 (来自 adata %d 只股票)", len(idx), len(df))
    except Exception as e:
        logger.warning("构建股票名称索引失败(adata): %s", e)

    # 清理冲突项
    idx = {k: v for k, v in idx.items() if v is not None}

    # 来源2: 候选池实时数据补充
    try:
        data = select_stocks(collector=collector)
        for pool_name in ["pool_a", "pool_b"]:
            for s in data.get(pool_name, []):
                code = s.get("code", "")
                name = s.get("name", "")
                if code and name:
                    idx[name] = code
    except Exception:
        pass

    _stock_name_index = idx
    logger.info("股票名称索引最终: %d 条", len(idx))
    return idx


def _resolve_stock_code(text: str) -> Optional[tuple[str, str]]:
    """从文本中解析股票，返回 (代码, 名称) 或 None。

    支持：
    - 6位数字代码：如 "600519"
    - 完整股票名称：如 "贵州茅台"
    - 简称/品牌名：如 "茅台"、"宁德"
    - 名称+诊断意图：如 "分析一下茅台"
    """
    if not text:
        return None

    # 1. 6位数字代码优先
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
    if m:
        code = m.group(1)
        # 尝试获取名称
        idx = _build_stock_name_index()
        name = ""
        for n, c in idx.items():
            if c == code and len(n) >= 4:
                name = n
                break
        logger.info("StockResolve: 代码匹配 %s -> %s", code, name or "?")
        return (code, name)

    # 2. 名称匹配：从索引中查找
    idx = _build_stock_name_index()
    if not idx:
        return None

    # 按名称长度降序匹配（优先完整名称，避免 "茅台" 匹配到 "贵州茅台" 之前误匹配其他）
    for name in sorted(idx.keys(), key=len, reverse=True):
        if name in text:
            code = idx[name]
            logger.info("StockResolve: 名称匹配 '%s' -> %s", name, code)
            return (code, name)

    return None


def _extract_stock_code(text):
    """从文本中提取6位股票代码（兼容旧接口，内部调用 _resolve_stock_code）。"""
    result = _resolve_stock_code(text)
    return result[0] if result else None

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
    resolved = _resolve_stock_code(user_message)
    code = resolved[0] if resolved else None
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


@app.route("/api/stock/search")
def api_stock_search():
    """搜索股票（支持代码或名称模糊匹配）。前端用于诊断前解析股票名称。

    支持两种查询模式：
    1. 精确查询："茅台"、"600519" → 直接匹配
    2. 全文查询："分析一下贵州茅台" → 从消息中提取股票名称再匹配

    GET /api/stock/search?q=茅台
    → {"matches": [{"code": "600519", "name": "贵州茅台"}, ...], "best": {"code": "600519", "name": "贵州茅台"}}
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"matches": [], "best": None})

    idx = _build_stock_name_index()
    matches = []

    # 1. 精确代码匹配
    if re.match(r'^\d{6}$', q):
        for name, code in idx.items():
            if code == q and len(name) >= 4:
                matches.append({"code": code, "name": name})
                break

    # 2. 名称包含匹配（q 是短名称，在完整名称中查找，如 "茅台" in "贵州茅台"）
    if not matches:
        seen = set()
        for name in sorted(idx.keys(), key=len, reverse=True):
            if len(seen) >= 20:
                break
            if q in name:
                code = idx[name]
                if code not in seen:
                    seen.add(code)
                    matches.append({"code": code, "name": name})

    # 3. 全文反向匹配（q 是完整消息如"分析一下贵州茅台"，从中提取已知股票名称）
    if not matches and len(q) > 4:
        seen = set()
        for name in sorted(idx.keys(), key=len, reverse=True):
            if len(name) < 2 or len(seen) >= 10:
                continue
            if name in q:
                code = idx[name]
                if code not in seen:
                    seen.add(code)
                    matches.append({"code": code, "name": name})

    # 4. 候选池补充
    if not matches:
        try:
            data = select_stocks(collector=collector)
            for pool_name in ["pool_a", "pool_b"]:
                for s in data.get(pool_name, []):
                    name = s.get("name", "")
                    if (q in name or (len(q) > 4 and name in q)) and len(matches) < 5:
                        matches.append({"code": s.get("code", ""), "name": name})
        except Exception:
            pass

    best = matches[0] if matches else None
    logger.info("StockSearch: q='%s' → %d matches, best=%s", q, len(matches), best)
    return jsonify({"matches": matches, "best": best})


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


# ── Agent 模式端点（Function Calling + 记忆 + 输出校验）──────

@app.route("/api/agent/chat/agent", methods=["POST"])
def api_agent_chat_agent():
    """具备工具调用能力的 Agent 对话（ReAct 循环 + 服务端记忆 + 输出校验）。

    与 /api/agent/chat 的区别：LLM 自主决定调用哪些工具获取实时数据，
    而非后端预先塞满上下文；对话记忆服务端持久化；输出经规则校验。
    """
    body = request.get_json(silent=True) or {}
    user_message = (body.get("message") or "").strip()
    session_id = (body.get("session_id") or "default").strip()
    if not user_message:
        return jsonify({"error": "消息不能为空"}), 400

    try:
        from .agent_tools import ToolContext
        from .agent_memory import get_agent_memory
    except ImportError:
        from agent_tools import ToolContext  # type: ignore
        from agent_memory import get_agent_memory  # type: ignore

    mem = get_agent_memory()
    agent = get_agent()

    # 工具上下文：注入真实数据能力
    tool_ctx = ToolContext(
        collector=collector,
        extract_stock_context=_extract_stock_context,
        select_stocks=select_stocks,
        get_signal_store=get_signal_store,
    )

    # 服务端会话记忆 + 用户画像
    history = mem.get_history(session_id)
    # 长对话滚动摘要：把更早的历史压缩成摘要，避免 recent window 截断失忆
    mem.maybe_summarize(session_id, agent.summarize_history)
    summary = mem.get_summary(session_id)

    # 记录用户问的股票 + 交易风格（画像）
    resolved = _resolve_stock_code(user_message)
    if resolved:
        mem.note_asked_stock(resolved[0], resolved[1])
    mem.infer_and_note_style(user_message)
    profile_ctx = mem.get_profile_context()
    base_ctx = profile_ctx
    if summary:
        base_ctx = (f"【历史对话摘要】{summary}\n" + profile_ctx).strip()

    qt = agent._classify_question(user_message, resolved[0] if resolved else "")
    result = agent.chat_agent(
        user_message, tool_ctx,
        chat_history=history,
        base_context=base_ctx,
        question_type=qt,
    )
    if not result:
        # 降级：回退到普通 chat（保证可用性）
        data = select_stocks(collector=collector)
        all_c = data.get("pool_a", []) + data.get("pool_b", [])
        reply = agent.chat(
            user_message, all_c, data.get("hot_sectors", []),
            get_signal_store().get_all(), chat_history=history,
            breadth=data.get("market_breadth", 0.5),
        )
        if not reply:
            return jsonify({"error": "Agent 不可用", "fallback": True}), 200
        result = {"reply": reply, "tool_trace": [], "iterations": 0,
                  "fallback": True, "usage": {}, "corrected": False}

    # 输出质量校验（不通过仅标记，不阻断返回）
    validation = agent.validate_output(result.get("reply", ""), qt)

    # 持久化本轮对话
    mem.append_message(session_id, "user", user_message)
    mem.append_message(session_id, "assistant", result.get("reply", ""))

    return jsonify({
        "reply": result.get("reply", ""),
        "tool_trace": result.get("tool_trace", []),
        "iterations": result.get("iterations", 0),
        "validation": validation,
        "corrected": result.get("corrected", False),
        "usage": result.get("usage", {}),
        "fallback": result.get("fallback", False),
    })


@app.route("/api/agent/chat/agent/stream", methods=["POST"])
def api_agent_chat_agent_stream():
    """Agent 对话的 SSE 流式版本 — 推送工具调用过程 + 增量回答 + 成本。

    事件类型（SSE data JSON 的 type 字段）：
      tool   : {type:"tool", label}          Agent 正在调用某工具的过程提示
      chunk  : {type:"chunk", data}           最终回答的增量文本
      done   : {type:"done", tool_trace, iterations, usage, corrected, validation}
      error  : {type:"error"}                 LLM 不可用（前端可提示重试或走非流式）
    """
    body = request.get_json(silent=True) or {}
    user_message = (body.get("message") or "").strip()
    session_id = (body.get("session_id") or "default").strip()
    if not user_message:
        return jsonify({"error": "消息不能为空"}), 400

    try:
        from .agent_tools import ToolContext
        from .agent_memory import get_agent_memory
    except ImportError:
        from agent_tools import ToolContext  # type: ignore
        from agent_memory import get_agent_memory  # type: ignore

    mem = get_agent_memory()
    agent = get_agent()
    tool_ctx = ToolContext(
        collector=collector,
        extract_stock_context=_extract_stock_context,
        select_stocks=select_stocks,
        get_signal_store=get_signal_store,
    )

    history = mem.get_history(session_id)
    mem.maybe_summarize(session_id, agent.summarize_history)
    summary = mem.get_summary(session_id)
    resolved = _resolve_stock_code(user_message)
    if resolved:
        mem.note_asked_stock(resolved[0], resolved[1])
    mem.infer_and_note_style(user_message)
    profile_ctx = mem.get_profile_context()
    base_ctx = profile_ctx
    if summary:
        base_ctx = (f"【历史对话摘要】{summary}\n" + profile_ctx).strip()
    qt = agent._classify_question(user_message, resolved[0] if resolved else "")

    def generate():
        full_reply = ""
        meta = {"tool_trace": [], "iterations": 0, "usage": {}, "corrected": False}
        errored = False
        try:
            for ev in agent.chat_agent_stream(
                user_message, tool_ctx,
                chat_history=history, base_context=base_ctx, question_type=qt,
            ):
                t = ev.get("type")
                if t == "tool_start":
                    yield f"data: {json.dumps({'type': 'tool', 'label': ev.get('label', '')}, ensure_ascii=False)}\n\n"
                elif t == "chunk":
                    full_reply += ev.get("data", "")
                    yield f"data: {json.dumps({'type': 'chunk', 'data': ev.get('data', '')}, ensure_ascii=False)}\n\n"
                elif t == "done":
                    full_reply = ev.get("reply", full_reply)
                    meta.update({
                        "tool_trace": ev.get("tool_trace", []),
                        "iterations": ev.get("iterations", 0),
                        "usage": ev.get("usage", {}),
                        "corrected": ev.get("corrected", False),
                    })
                elif t == "error":
                    errored = True
        except Exception as e:
            logger.error("Agent stream 异常: %s", e)
            errored = True

        # LLM 不可用 → 降级到非流式 chat，一次性推回
        if errored and not full_reply:
            try:
                data = select_stocks(collector=collector)
                all_c = data.get("pool_a", []) + data.get("pool_b", [])
                reply = agent.chat(
                    user_message, all_c, data.get("hot_sectors", []),
                    get_signal_store().get_all(), chat_history=history,
                    breadth=data.get("market_breadth", 0.5),
                ) or "抱歉，暂时无法处理该请求，请稍后重试。"
                full_reply = reply
                yield f"data: {json.dumps({'type': 'chunk', 'data': reply}, ensure_ascii=False)}\n\n"
                meta["fallback"] = True
            except Exception:
                yield f"data: {json.dumps({'type': 'chunk', 'data': '抱歉，服务暂时不可用。'}, ensure_ascii=False)}\n\n"

        # 持久化 + 校验
        if full_reply:
            mem.append_message(session_id, "user", user_message)
            mem.append_message(session_id, "assistant", full_reply)
        validation = agent.validate_output(full_reply, qt)
        done_payload = {"type": "done", "validation": validation, **meta}
        yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

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


@app.route("/api/loop/tuning")
def api_loop_tuning():
    """反馈闭环调节报告 — 展示规则引擎因实盘表现「自调」了哪些信号权重。

    可解释性出口：每个信号的历史样本数/胜率/均收益 → 生成的评分乘数。
    """
    try:
        from .weight_tuner import get_tuning_report
    except ImportError:
        from weight_tuner import get_tuning_report  # type: ignore
    return jsonify(get_tuning_report(store=get_decision_store()))


@app.route("/api/agent/eval", methods=["POST"])
def api_agent_eval():
    """跑 Agent 离线评估集，返回评估报告（回答「怎么证明 Agent 好不好」）。

    body: {"limit": N}  可选，只跑前 N 个 case（快速冒烟）。
    评估在 mock 工具上下文下运行，聚焦推理链路与工具选择质量。
    """
    body = request.get_json(silent=True) or {}
    limit = body.get("limit")
    runs = body.get("runs", 1)
    try:
        from .agent_eval import run_eval
    except ImportError:
        from agent_eval import run_eval  # type: ignore
    return jsonify(run_eval(limit=int(limit) if limit else None, runs=int(runs)))



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


# ── 持仓盯盘助手 ──────────────────────────────────────────────

@app.route("/api/watch/positions", methods=["GET"])
def api_watch_list():
    """查询当前盯盘持仓列表（含实时价与浮盈）。"""
    return jsonify({"positions": position_watcher.get_positions_enriched()})


@app.route("/api/watch/positions", methods=["POST"])
def api_watch_add():
    """新增盯盘持仓。支持手动添加任意股票；点位按信号自动、可手动覆盖。"""
    body = request.get_json(silent=True) or {}
    result = position_watcher.add_position(
        code=str(body.get("code", "")).strip(),
        name=str(body.get("name", "")).strip(),
        cost=float(body.get("cost", 0) or 0),
        signal=str(body.get("signal", "")).strip(),
        sector=str(body.get("sector", "")).strip(),
        take_profit_pct=(float(body["take_profit_pct"])
                         if body.get("take_profit_pct") not in (None, "") else None),
        stop_loss_pct=(float(body["stop_loss_pct"])
                       if body.get("stop_loss_pct") not in (None, "") else None),
        source=str(body.get("source", "manual")).strip() or "manual",
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/watch/positions/<int:pid>", methods=["PATCH"])
def api_watch_update(pid: int):
    """手动修改持仓点位。"""
    body = request.get_json(silent=True) or {}
    result = position_watcher.update_stops(
        pid,
        take_profit_pct=(float(body["take_profit_pct"])
                         if body.get("take_profit_pct") not in (None, "") else None),
        stop_loss_pct=(float(body["stop_loss_pct"])
                       if body.get("stop_loss_pct") not in (None, "") else None),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/watch/positions/<int:pid>", methods=["DELETE"])
def api_watch_remove(pid: int):
    """移除盯盘持仓。"""
    return jsonify(position_watcher.remove_position(pid))

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

# 启动时检查是否有应生成的报告（如启动时已在 9:25 之后但盘前简报未生成）
def _auto_generate_reports():
    try:
        generated = check_and_generate()
        if generated:
            logger.info("启动时自动生成报告: %s", generated)
    except Exception as e:
        logger.warning("自动报告生成检查失败: %s", e)

_auto_generate_reports()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("启动看板服务 v2: http://127.0.0.1:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
