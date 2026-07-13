#!/usr/bin/env python3
"""
研究报告存储与生成引擎

提供:
  - SQLite 持久化存储（报告 CRUD）
  - 各时段报告自动生成（盘前/早盘/午盘/全日/周度）
  - 结构化数据 + Markdown 双格式输出

报告类型:
  pre_market      — 09:25 盘前速览
  morning_close   — 11:30 早盘收盘
  midday_preview  — 12:55 午盘前瞻
  full_day_review — 15:05 全日复盘
  weekly_summary  — 周五 15:30 周度总结
  signal_review   — 信号回测简报（手动触发）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "reports.db"

REPORT_TYPES = [
    "pre_market",
    "morning_close",
    "midday_preview",
    "full_day_review",
    "weekly_summary",
    "signal_review",
]

REPORT_META = {
    "pre_market":      {"title": "盘前速览",   "icon": "wb_twilight",  "time": "09:25", "hour": 9,  "minute": 25},
    "morning_close":   {"title": "早盘收盘",   "icon": "wb_sunny",     "time": "11:30", "hour": 11, "minute": 30},
    "midday_preview":  {"title": "午盘前瞻",   "icon": "light_mode",   "time": "12:55", "hour": 12, "minute": 55},
    "full_day_review": {"title": "全日复盘",   "icon": "nights_stay",  "time": "15:05", "hour": 15, "minute": 5},
    "weekly_summary":  {"title": "周度总结",   "icon": "calendar_month","time": "周五",  "hour": 15, "minute": 30},
    "signal_review":   {"title": "信号回测",   "icon": "query_stats",  "time": "手动",  "hour": 0,  "minute": 0},
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class ReportStore:
    """研究报告持久化存储 + 自动生成引擎。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    report_date TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'generated',
                    UNIQUE(report_type, report_date)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type)")
            conn.commit()
            conn.close()

    # ── CRUD ──────────────────────────────────────────────

    def save(self, report_type: str, content: str, data: dict | None = None,
             report_date: str | None = None) -> int:
        """保存/更新报告。返回 report id。"""
        if report_type not in REPORT_TYPES:
            raise ValueError(f"未知报告类型: {report_type}")
        if report_date is None:
            report_date = _today_str()
        title = REPORT_META.get(report_type, {}).get("title", report_type)
        data_json = json.dumps(data or {}, ensure_ascii=False)

        with self._lock:
            conn = sqlite3.connect(str(DB_PATH))
            existing = conn.execute(
                "SELECT id FROM reports WHERE report_type=? AND report_date=?",
                (report_type, report_date)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE reports SET title=?, content=?, data_json=?, generated_at=?, status='updated' WHERE id=?",
                    (title, content, data_json, _now_iso(), existing[0])
                )
                rid = existing[0]
            else:
                cur = conn.execute(
                    "INSERT INTO reports (report_type, title, content, data_json, report_date, generated_at, status) VALUES (?,?,?,?,?,?,?)",
                    (report_type, title, content, data_json, report_date, _now_iso(), "generated")
                )
                rid = cur.lastrowid
            conn.commit()
            conn.close()
        logger.info("报告已保存: %s %s (id=%s)", report_type, report_date, rid)
        return rid

    def get_by_date(self, report_date: str | None = None) -> list[dict]:
        """获取指定日期的所有报告。None = 今天。"""
        if report_date is None:
            report_date = _today_str()
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM reports WHERE report_date=? ORDER BY id",
            (report_date,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_by_type(self, report_type: str, report_date: str | None = None) -> dict | None:
        """获取指定类型的单个报告。"""
        if report_date is None:
            report_date = _today_str()
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reports WHERE report_type=? AND report_date=?",
            (report_type, report_date)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_by_id(self, report_id: int) -> dict | None:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_available_dates(self, limit: int = 30) -> list[str]:
        """返回有报告的日期列表（倒序）。"""
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT DISTINCT report_date FROM reports ORDER BY report_date DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def get_latest(self) -> dict | None:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reports ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def exists(self, report_type: str, report_date: str | None = None) -> bool:
        if report_date is None:
            report_date = _today_str()
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT 1 FROM reports WHERE report_type=? AND report_date=?",
            (report_type, report_date)
        ).fetchone()
        conn.close()
        return row is not None

    # ── 报告生成器 ────────────────────────────────────────

    def _collect_context(self) -> dict:
        """收集当前市场上下文数据，供报告生成使用。"""
        ctx: dict = {
            "time": _now_iso(),
            "date": _today_str(),
            "indices": [],
            "market_breadth": 0.5,
            "sentiment": {},
            "top_sectors": [],
            "stock_picks": {"pool_a": [], "pool_b": []},
            "top_flow_stocks": [],
            "pre_market": {},
            "signal_stats": {},
            "daily_scores_summary": {},
        }

        # ── 大盘指数 ──
        try:
            from .data_fetcher import fetch_northbound_flow, _MOCK_STOCKS
        except ImportError:
            from data_fetcher import fetch_northbound_flow, _MOCK_STOCKS  # type: ignore[no-redef]

        try:
            import requests as _rq
            s = _rq.Session(); s.trust_env = False
            resp = s.get("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006", timeout=5)
            resp.encoding = 'gbk'
            for line in resp.text.strip().split(';\n'):
                if '=' not in line: continue
                _, value = line.split('=', 1)
                value = value.strip().strip('"').strip("'")
                fields = value.split('~')
                if len(fields) < 33: continue
                amount = float(fields[37]) if len(fields) > 37 and fields[37] else 0
                ctx["indices"].append({
                    "name": fields[1],
                    "code": fields[2],
                    "price": round(float(fields[3]), 2) if fields[3] else 0,
                    "change_pct": round(float(fields[32]), 2) if fields[32] else 0,
                    "amount": round(amount / 1e4),  # 万元→亿
                })
        except Exception as e:
            logger.debug("报告上下文-指数获取失败: %s", e)

        # ── 市场广度 + 情绪 ──
        try:
            from .signals import get_signal_store
            store = get_signal_store()
            signals = store.get_all()
            sent_data = signals.get("sentiment", {}).get("data", {})
            market = sent_data.get("market", {})
            ctx["sentiment"] = {
                "sentiment_index": market.get("sentiment_index"),
                "po_ban_rate": market.get("po_ban_rate"),
                "rotation_speed": market.get("rotation_speed"),
            }
        except Exception as e:
            logger.debug("报告上下文-情绪获取失败: %s", e)

        # ── 板块排名 ──
        try:
            from . import collector as _col
        except ImportError:
            import collector as _col  # type: ignore[no-redef]
        if hasattr(_col, 'collector') and _col.collector:
            try:
                dash = _col.collector.get_dashboard_data(sector_type="watch")
                ctx["top_sectors"] = dash.get("rank", [])[:10]
            except Exception:
                pass

        # ── 候选池 ──
        try:
            from .stock_selector import select_stocks
        except ImportError:
            from stock_selector import select_stocks  # type: ignore[no-redef]
        try:
            picks = select_stocks(collector=_col.collector if hasattr(_col, 'collector') else None)
            ctx["stock_picks"] = {
                "pool_a": picks.get("pool_a", [])[:8],
                "pool_b": picks.get("pool_b", [])[:8],
            }
            ctx["market_breadth"] = picks.get("market_breadth", 0.5)
        except Exception as e:
            logger.debug("报告上下文-选股获取失败: %s", e)

        # ── 盘前数据 ──
        try:
            from .pre_market import PreMarketScanner
        except ImportError:
            from pre_market import PreMarketScanner  # type: ignore[no-redef]
        try:
            scanner = PreMarketScanner()
            ctx["pre_market"] = scanner.scan()
        except Exception:
            pass

        # ── 日评分摘要 ──
        try:
            from .daily_scorer import load_daily_scores
            scores = load_daily_scores()
            if scores:
                avg = sum(v["combined_score"] for v in scores.values()) / len(scores)
                top10 = sorted(scores.items(), key=lambda x: x[1]["combined_score"], reverse=True)[:10]
                ctx["daily_scores_summary"] = {
                    "total": len(scores),
                    "avg_score": round(avg, 1),
                    "top10": [{"code": c, "score": s["combined_score"],
                               "signals": s.get("signal_names", "")} for c, s in top10],
                }
        except Exception:
            pass

        # ── 信号统计 ──
        try:
            from .decision_store import get_decision_store
            ds = get_decision_store()
            ctx["signal_stats"] = ds.get_total_stats()
        except Exception:
            pass

        return ctx

    def generate_pre_market(self, force: bool = False) -> dict | None:
        """生成盘前速览报告（09:25）。"""
        if not force and self.exists("pre_market"):
            return self.get_by_type("pre_market")

        ctx = self._collect_context()
        pre = ctx.get("pre_market", {})
        overnight = pre.get("overnight", {})
        indices = overnight.get("indices", {})
        picks = ctx.get("stock_picks", {})
        pool_a = picks.get("pool_a", [])
        pool_b = picks.get("pool_b", [])

        # ── 昨日回顾 ──
        yesterday_review = self._build_yesterday_review(ctx)

        # ── 今日判断 ──
        overall = overnight.get("overall_sentiment", 0)
        if overall > 0.3:
            posture = "偏进攻"
            position = "5-6成"
            rhythm = "开盘观察15分钟，回踩确认后加仓"
        elif overall < -0.3:
            posture = "偏防守"
            position = "2-3成"
            rhythm = "观望为主，等盘中企稳信号"
        else:
            posture = "均衡"
            position = "3-5成"
            rhythm = "等待盘中方向确认，不急于动手"

        # ── 外盘映射 ──
        idx_label = {"nasdaq": "纳斯达克", "sp500": "标普500", "dow": "道指", "hsi": "恒生", "a50": "A50期货"}
        overnight_list = []
        for k, v in indices.items():
            overnight_list.append({"name": idx_label.get(k, k), "pct_chg": round(v, 2) if isinstance(v, (int, float)) else v})

        pos_secs = overnight.get("positive_sectors", [])[:8]
        neg_secs = overnight.get("negative_sectors", [])[:5]

        # ── 大盘关键位（从历史K线估算）──
        key_levels = self._estimate_key_levels(ctx)

        # ── 竞价情绪 ──
        auction = pre.get("auction", [])
        auction_breadth = {"up_ratio": ctx.get("market_breadth", 0.5), "total": len(auction) if auction else 0}
        auc_status = "偏暖" if auction_breadth["up_ratio"] > 0.55 else ("偏冷" if auction_breadth["up_ratio"] < 0.45 else "中性")

        # ── 重点关注板块 ──
        hot_sectors = []
        top_secs = ctx.get("top_sectors", [])
        for s in top_secs[:6]:
            nm = s.get("name", "")
            v = s.get("value", 0) or 0
            pct = s.get("pct_chg", 0) or 0
            reason = ""
            if nm in pos_secs:
                reason = "外盘映射利好"
            elif v > 5:
                reason = "资金主动流入"
            elif pct > 2:
                reason = "竞价涨幅居前"
            hot_sectors.append({
                "name": nm, "value": round(v, 1), "pct_chg": round(pct, 2) if pct else 0,
                "reason": reason,
            })

        # ── 重点关注个股 ──
        key_stocks = []
        seen_codes = set()
        for s in pool_a[:3] + pool_b[:2]:
            code = s.get("code", "")
            if code in seen_codes: continue
            seen_codes.add(code)
            key_stocks.append({
                "name": s.get("name", ""), "code": code,
                "pct_chg": s.get("pct_chg", 0) or 0,
                "score": s.get("score", 0) or 0,
                "signal": s.get("signal", ""),
                "note": "A池追涨" if s in pool_a else "B池低吸",
            })

        # ── 今日避雷 ──
        risk_warnings = []
        for s in neg_secs[:4]:
            risk_warnings.append({"name": s, "reason": "外盘映射承压"})
        # 补充：检查北向资金持续流出方向
        # (简化实现，实际可接入北向数据)

        # ── 今日事件 ──
        today_events = self._get_today_events()

        data = {
            "yesterday_review": yesterday_review,
            "today_judgment": {
                "sentiment": overnight.get("summary", ""),
                "posture": posture,
                "position": position,
                "rhythm": rhythm,
            },
            "overnight_indices": overnight_list,
            "sector_mapping": {"positive": pos_secs, "negative": neg_secs},
            "key_levels": key_levels,
            "auction_sentiment": {
                "breadth": auction_breadth,
                "hot_count": len(auction),
                "status": auc_status,
            },
            "hot_sectors": hot_sectors,
            "key_stocks": key_stocks,
            "risk_warnings": risk_warnings,
            "today_events": today_events,
            "pool_summary": {"a_count": len(pool_a), "b_count": len(pool_b)},
        }

        content = self._build_pre_market_text(ctx)
        rid = self.save("pre_market", content, data)
        return self.get_by_id(rid)

    def generate_morning_close(self, force: bool = False) -> dict | None:
        """生成早盘收盘报告（11:30）。"""
        if not force and self.exists("morning_close"):
            return self.get_by_type("morning_close")

        ctx = self._collect_context()
        top_sectors = ctx.get("top_sectors", [])
        picks = ctx.get("stock_picks", {})
        pool_a = picks.get("pool_a", [])
        pool_b = picks.get("pool_b", [])
        indices = ctx.get("indices", [])
        sentiment = ctx.get("sentiment", {})

        # ── 上午复盘 vs 盘前预判 ──
        pre_report = self.get_by_type("pre_market")
        pre_judgment = ""
        if pre_report:
            pre_data = json.loads(pre_report.get("data_json", "{}"))
            pre_judgment = pre_data.get("today_judgment", {}).get("posture", "")

        # ── 板块资金 ──
        sector_flow = []
        for s in top_sectors[:8]:
            sector_flow.append({
                "name": s.get("name", ""),
                "value": round(s.get("value", 0) or 0, 1),
                "pct_chg": round(s.get("pct_chg", 0) or 0, 2),
                "color": s.get("color", ""),
            })

        # ── 市场指标 ──
        total_amt = 0
        for idx in indices:
            amt = idx.get("amount", 0) or 0
            if idx.get("code") != "399006":
                total_amt += amt
        total_amt_yi = round(total_amt) if total_amt > 0 else None

        # ── 下午预判 ──
        breadth = ctx.get("market_breadth", 0.5)
        if breadth > 0.55:
            afternoon = "资金有持续迹象，下午偏乐观"
            watch = ["强势板块是否能延续", "成交量是否萎缩", "尾盘是否有资金出逃"]
        elif breadth < 0.4:
            afternoon = "上午偏弱，下午观望等企稳"
            watch = ["是否有抄底资金入场", "跌幅是否收窄", "防御板块是否走强"]
        else:
            afternoon = "方向不明，等待尾盘确认"
            watch = ["板块轮动方向", "北向资金动向", "2点后资金选择"]

        data = {
            "morning_review": {
                "vs_prediction": f"盘前判{pre_judgment}" if pre_judgment else "",
                "summary": f"市场广度{breadth:.0%}，上午资金{'集中' if len(sector_flow) >= 3 else '分散'}",
            },
            "indices": [{"name": i.get("name",""), "price": i.get("price"),
                         "change_pct": i.get("change_pct", 0)} for i in indices[:3]],
            "sector_flow": sector_flow,
            "market_metrics": {
                "breadth": round(breadth, 2),
                "sentiment_index": sentiment.get("sentiment_index"),
                "po_ban_rate": sentiment.get("po_ban_rate"),
                "total_amount": total_amt_yi,
            },
            "pool_status": {
                "a_count": len(pool_a), "b_count": len(pool_b),
                "a_top": [{"name": s.get("name",""), "code": s.get("code",""),
                           "score": s.get("score",0), "pct_chg": s.get("pct_chg",0)}
                          for s in pool_a[:4]],
                "b_top": [{"name": s.get("name",""), "code": s.get("code",""),
                           "score": s.get("score",0), "pct_chg": s.get("pct_chg",0)}
                          for s in pool_b[:4]],
            },
            "afternoon_outlook": {
                "prediction": afternoon,
                "watch_points": watch,
            },
        }

        content = self._build_morning_close_text(ctx)
        rid = self.save("morning_close", content, data)
        return self.get_by_id(rid)

    def generate_midday_preview(self, force: bool = False) -> dict | None:
        """生成午盘前瞻报告（12:55）。"""
        if not force and self.exists("midday_preview"):
            return self.get_by_type("midday_preview")

        ctx = self._collect_context()
        top_sectors = ctx.get("top_sectors", [])
        picks = ctx.get("stock_picks", {})
        pool_a = picks.get("pool_a", [])
        pool_b = picks.get("pool_b", [])
        breadth = ctx.get("market_breadth", 0.5)

        # 判断下午策略
        if breadth > 0.55:
            posture = "进攻延续"
            focus = [s.get("name","") for s in top_sectors[:3]]
            note = "上午强势方向大概率延续，关注量能变化"
        elif breadth < 0.4:
            posture = "防守为主"
            focus = [s.get("name","") for s in top_sectors[:3]] if top_sectors else []
            note = "下午可能有抄底资金，关注2点后方向选择"
        else:
            posture = "均衡应对"
            focus = [s.get("name","") for s in top_sectors[:3]]
            note = "方向不明，以观望为主，尾盘半小时定方向"

        data = {
            "morning_summary": {
                "highlights": f"上午热板块: {', '.join(s.get('name','') for s in top_sectors[:3])}" if top_sectors else "上午暂无突出热点",
                "breadth": round(breadth, 2),
            },
            "afternoon_strategy": {
                "posture": posture,
                "focus_sectors": focus,
                "note": note,
                "watch_points": ["2:00-2:30 资金方向选择", "尾盘15分钟量能", "是否有新板块接力"],
            },
            "pool_status": {"a_count": len(pool_a), "b_count": len(pool_b)},
            "top_sectors": [{"name": s.get("name",""), "value": round(s.get("value",0) or 0, 1),
                            "pct_chg": round(s.get("pct_chg",0) or 0, 2)}
                           for s in top_sectors[:6]],
        }

        content = self._build_midday_preview_text(ctx)
        rid = self.save("midday_preview", content, data)
        return self.get_by_id(rid)

    def generate_full_day_review(self, force: bool = False) -> dict | None:
        """生成全日复盘报告（15:05）。"""
        if not force and self.exists("full_day_review"):
            return self.get_by_type("full_day_review")

        ctx = self._collect_context()
        top_sectors = ctx.get("top_sectors", [])
        picks = ctx.get("stock_picks", {})
        pool_a = picks.get("pool_a", [])
        pool_b = picks.get("pool_b", [])
        scores = ctx.get("daily_scores_summary", {})
        indices = ctx.get("indices", [])
        sentiment = ctx.get("sentiment", {})
        breadth = ctx.get("market_breadth", 0.5)

        # 今日总评
        total_amt = sum((i.get("amount", 0) or 0) for i in indices if i.get("code") != "399006")
        total_amt_yi = round(total_amt) if total_amt > 0 else None

        if breadth > 0.55:
            verdict = "今日偏强，资金有方向"
        elif breadth < 0.4:
            verdict = "今日偏弱，防御情绪主导"
        else:
            verdict = "今日震荡，方向不明确"

        # 板块资金
        sector_flow_final = []
        for s in top_sectors[:10]:
            sector_flow_final.append({
                "name": s.get("name", ""),
                "value": round(s.get("value", 0) or 0, 1),
                "pct_chg": round(s.get("pct_chg", 0) or 0, 2),
            })

        # 次日预判
        if breadth > 0.55:
            next_sentiment = "偏乐观"
            next_focus = "今日强势板块明天是否有溢价"
            next_risk = "追高风险，注意高位标的兑现压力"
        elif breadth < 0.4:
            next_sentiment = "偏谨慎"
            next_focus = "是否有超跌反弹机会，B池低吸标的"
            next_risk = "惯性下跌风险，不在大跌日接飞刀"
        else:
            next_sentiment = "中性"
            next_focus = "等待晚间消息面和外盘给出方向"
            next_risk = "震荡市中频繁交易损耗大"

        # 候选池表现
        pool_rising = len([s for s in pool_a + pool_b if (s.get("pct_chg", 0) or 0) > 0])
        pool_total = len(pool_a) + len(pool_b)

        data = {
            "day_summary": {
                "verdict": verdict,
                "breadth": round(breadth, 2),
                "total_amount": total_amt_yi,
            },
            "indices_final": [{"name": i.get("name",""), "price": i.get("price"),
                               "change_pct": i.get("change_pct", 0)} for i in indices[:3]],
            "sector_flow_final": sector_flow_final,
            "market_metrics": {
                "sentiment_index": sentiment.get("sentiment_index"),
                "po_ban_rate": sentiment.get("po_ban_rate"),
                "breadth": round(breadth, 2),
                "total_amount": total_amt_yi,
            },
            "pool_performance": {
                "a_count": len(pool_a), "b_count": len(pool_b),
                "rising_count": pool_rising, "total": pool_total,
                "a_top": [{"name": s.get("name",""), "code": s.get("code",""),
                           "score": s.get("score",0), "pct_chg": s.get("pct_chg",0)}
                          for s in pool_a[:5]],
                "b_top": [{"name": s.get("name",""), "code": s.get("code",""),
                           "score": s.get("score",0), "pct_chg": s.get("pct_chg",0)}
                          for s in pool_b[:5]],
            },
            "daily_scores_summary": {
                "total": scores.get("total", 0),
                "avg_score": scores.get("avg_score", 0),
                "top5": scores.get("top10", [])[:5],
            },
            "next_day_preview": {
                "sentiment": next_sentiment,
                "focus": next_focus,
                "risk": next_risk,
            },
        }

        content = self._build_full_day_text(ctx)
        rid = self.save("full_day_review", content, data)
        return self.get_by_id(rid)

    def generate_weekly_summary(self, force: bool = False) -> dict | None:
        """生成周度总结报告（周五 15:30）。"""
        today = date.today()
        if not force:
            if today.weekday() != 4:
                return None
            if self.exists("weekly_summary"):
                return self.get_by_type("weekly_summary")

        week_start = today - timedelta(days=today.weekday())
        week_dates = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d")
                      for i in range(5) if week_start + timedelta(days=i) <= today]

        ctx = self._collect_context()
        top_sectors = ctx.get("top_sectors", [])
        scores = ctx.get("daily_scores_summary", {})

        data = {
            "week_start": week_start.strftime("%Y-%m-%d"),
            "week_end": today.strftime("%Y-%m-%d"),
            "days_covered": len(week_dates),
            "top_sectors": [{"name": s.get("name",""), "value": round(s.get("value",0) or 0, 1)}
                           for s in top_sectors[:10]],
            "sentiment": ctx.get("sentiment", {}),
            "signal_stats": ctx.get("signal_stats", {}),
            "daily_scores": scores,
            "market_indices": ctx.get("indices", []),
        }

        lines = [
            f"══ 周度总结 {week_start.strftime('%m/%d')}-{today.strftime('%m/%d')} ══",
            "", f"覆盖 {len(week_dates)} 个交易日", "",
            "【本周主线】",
        ]
        for r in top_sectors[:5]:
            lines.append(f"• {r.get('name','')}: {(r.get('value',0) or 0):+.1f}亿")
        if scores:
            lines.extend(["", f"【日评分】覆盖 {scores.get('total',0)} 只，均分 {scores.get('avg_score',0)}"])
        lines.extend(["", "【下周展望】", "结合本周轮动节奏和周末消息面综合研判。"])
        content = "\n".join(lines)
        rid = self.save("weekly_summary", content, data, report_date=today.strftime("%Y-%m-%d"))
        return self.get_by_id(rid)

    def generate_signal_review(self, force: bool = False) -> dict | None:
        """生成信号回测简报。"""
        ctx = self._collect_context()
        stats = ctx.get("signal_stats", {})

        data = {"signal_stats": stats, "generated_at": _now_iso()}

        lines = ["══ 信号回测简报 ══", "", f"生成时间: {_now_iso()}", ""]
        if stats:
            lines.append("【总体统计】")
            lines.append(f"总决策数: {stats.get('total', '?')}")
            lines.append(f"胜率: {stats.get('win_rate', '?')}")
            lines.append(f"平均收益: {stats.get('avg_return', '?')}")
            lines.append("")
            by_signal = stats.get("by_signal", {})
            if by_signal:
                lines.append("【按信号类型】")
                for sig, sdata in sorted(by_signal.items(), key=lambda x: x[1].get('win_rate', 0), reverse=True):
                    lines.append(f"• {sig}: 胜率 {sdata.get('win_rate','?')} | 次数 {sdata.get('count','?')} | 平均收益 {sdata.get('avg_return','?')}")
        else:
            lines.append("（暂无足够回测数据，需积累更多交易决策）")
        content = "\n".join(lines)
        rid = self.save("signal_review", content, data)
        return self.get_by_id(rid)

    # ── 辅助方法 ──────────────────────────────────────────

    def _build_yesterday_review(self, ctx: dict) -> dict:
        """生成昨日回顾。"""
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_reports = self.get_by_date(yesterday)
        if not yesterday_reports:
            return {"available": False, "text": "暂无昨日数据"}

        full_day = next((r for r in yesterday_reports if r["report_type"] == "full_day_review"), None)
        if not full_day:
            return {"available": False, "text": "昨日无复盘数据"}

        try:
            fd_data = json.loads(full_day.get("data_json", "{}"))
            next_day = fd_data.get("next_day_preview", {})
            indices_final = fd_data.get("indices_final", [])

            idx_summary = ""
            for i in indices_final[:2]:
                pct = i.get("change_pct", 0) or 0
                sign = "+" if pct >= 0 else ""
                idx_summary += f"{i['name']}{sign}{pct:.1f}% "

            return {
                "available": True,
                "date": yesterday,
                "prediction": next_day.get("sentiment", ""),
                "indices": idx_summary.strip(),
                "breadth": fd_data.get("market_metrics", {}).get("breadth"),
            }
        except Exception:
            return {"available": False, "text": "数据解析失败"}

    def _estimate_key_levels(self, ctx: dict) -> dict:
        """估算大盘关键支撑/阻力位。"""
        indices = ctx.get("indices", [])
        levels = {}
        for idx in indices:
            name = idx.get("name", "")
            price = idx.get("price", 0) or 0
            if price <= 0: continue
            # 简化：以当前价为基准，±2% 和 ±5% 为关键位
            levels[name] = {
                "resistance_2": round(price * 1.05, 0),
                "resistance_1": round(price * 1.02, 0),
                "price": round(price, 0),
                "support_1": round(price * 0.98, 0),
                "support_2": round(price * 0.95, 0),
            }
        return levels

    def _get_today_events(self) -> list[dict]:
        """获取今日重要事件（简化实现）。"""
        events = []
        # 检查是否有重要经济数据发布
        # (实际可接入财经日历 API)
        return events

    # ── 文本报告（兜底模板）─────────────────────────────

    def _build_pre_market_text(self, ctx: dict) -> str:
        pre = ctx.get("pre_market", {}).get("overnight", {})
        indices = pre.get("indices", {})
        lines = [
            "══ 盘前速览 ══",
            "",
            "【今日判断】",
        ]
        overall = pre.get("overall_sentiment", 0)
        if overall > 0.3:
            lines.append("外盘偏暖 + 竞价活跃 → 偏进攻 | 仓位5-6成 | 开盘观察后加仓")
        elif overall < -0.3:
            lines.append("外盘偏冷 → 偏防守 | 仓位2-3成 | 观望等企稳")
        else:
            lines.append("外盘中性 → 均衡 | 仓位3-5成 | 等待方向确认")

        lines.extend(["", "【外盘映射】"])
        label_map = {"nasdaq": "纳斯达克", "sp500": "标普500", "dow": "道指", "hsi": "恒生", "a50": "A50期货"}
        for k, v in indices.items():
            sign = "+" if v > 0 else ""
            lines.append(f"  • {label_map.get(k, k)}: {sign}{v:.1f}%")
        lines.append("")

        lines.append("【竞价情绪】")
        lines.append(f"  {pre.get('summary', '')}")
        pos = pre.get("positive_sectors", [])
        neg = pre.get("negative_sectors", [])
        if pos:
            lines.append(f"  利好: {', '.join(pos[:6])}")
        if neg:
            lines.append(f"  承压: {', '.join(neg[:4])}")
        lines.append("")

        lines.append("【重点关注板块】")
        top = ctx.get("top_sectors", [])
        for s in top[:6]:
            nm = s.get("name", "")
            v = s.get("value", 0) or 0
            lines.append(f"  • {nm} {(v>=0 and'+' or '')}{v:.1f}亿")
        lines.append("")

        picks = ctx.get("stock_picks", {})
        lines.append(f"候选池 A{len(picks.get('pool_a',[]))}只 + B{len(picks.get('pool_b',[]))}只")
        return "\n".join(lines)

    def _build_morning_close_text(self, ctx: dict) -> str:
        top = ctx.get("top_sectors", [])
        picks = ctx.get("stock_picks", {})
        lines = [
            "══ 早盘收盘 ══",
            "",
            f"市场广度: {ctx.get('market_breadth', 0.5):.0%}",
            "",
            "【板块资金 Top5】",
        ]
        for r in top[:5]:
            lines.append(f"• {r['name']}: {(r['value'] or 0):+.1f}亿")
        lines.append("")
        lines.append(f"追涨池 {len(picks.get('pool_a',[]))}只 | 低吸池 {len(picks.get('pool_b',[]))}只")
        lines.append("关注下午量能变化和尾盘资金方向。")
        return "\n".join(lines)

    def _build_midday_preview_text(self, ctx: dict) -> str:
        top = ctx.get("top_sectors", [])
        lines = [
            "══ 午盘前瞻 ══",
            "",
            "【上午总结】",
            f"热板块 Top3: {', '.join(r['name'] for r in top[:3])}",
            f"市场广度: {ctx.get('market_breadth', 0.5):.0%}",
            "",
            "【下午策略】",
            "关注2:00-2:30资金方向选择，尾盘半小时定方向。",
            "候选池已就绪，点击「盘中选股」查看实时标的。",
        ]
        return "\n".join(lines)

    def _build_full_day_text(self, ctx: dict) -> str:
        top = ctx.get("top_sectors", [])
        picks = ctx.get("stock_picks", {})
        scores = ctx.get("daily_scores_summary", {})
        lines = [
            "══ 全日复盘 ══",
            "",
            "【资金汇总】",
        ]
        for r in top[:6]:
            lines.append(f"• {r['name']}: {(r['value'] or 0):+.1f}亿")
        lines.append("")
        lines.append(f"追涨池 {len(picks.get('pool_a',[]))}只 | 低吸池 {len(picks.get('pool_b',[]))}只")
        if scores:
            lines.append(f"日评分覆盖 {scores.get('total', 0)}只，均分 {scores.get('avg_score', 0)}")
        lines.append("")
        lines.append("【次日预判】")
        lines.append("结合全日资金流向和隔夜外盘综合研判。")
        return "\n".join(lines)


# ── 全局单例 ──────────────────────────────────────────────

_report_store: ReportStore | None = None


def get_report_store() -> ReportStore:
    global _report_store
    if _report_store is None:
        _report_store = ReportStore()
    return _report_store


# ── 定时生成检查 ──────────────────────────────────────────

def _is_auction_done() -> bool:
    """是否已过集合竞价（>= 9:25）。"""
    now = datetime.now()
    return now.hour > 9 or (now.hour == 9 and now.minute >= 25)


def check_and_generate() -> list[str]:
    """根据当前时间自动生成应生成的报告。返回生成列表。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return []  # 周末不生成

    store = get_report_store()
    generated = []
    hhmm = now.hour * 60 + now.minute

    # 盘前速览 — 9:25-11:30
    if 9 * 60 + 25 <= hhmm < 11 * 60 + 30:
        if not store.exists("pre_market"):
            store.generate_pre_market()
            generated.append("pre_market")

    # 早盘收盘 — 11:30-12:55
    if 11 * 60 + 30 <= hhmm < 12 * 60 + 55:
        if not store.exists("morning_close"):
            store.generate_morning_close()
            generated.append("morning_close")

    # 午盘前瞻 — 12:55-15:00
    if 12 * 60 + 55 <= hhmm < 15 * 60:
        if not store.exists("midday_preview"):
            store.generate_midday_preview()
            generated.append("midday_preview")

    # 全日复盘 — 15:05 之后
    if hhmm >= 15 * 60 + 5:
        if not store.exists("full_day_review"):
            store.generate_full_day_review()
            generated.append("full_day_review")

    # 周度总结 — 周五 15:30
    if now.weekday() == 4 and hhmm >= 15 * 60 + 30:
        if not store.exists("weekly_summary"):
            store.generate_weekly_summary()
            generated.append("weekly_summary")

    return generated


# ── CLI ────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    import sys
    store = get_report_store()

    if "--generate" in sys.argv:
        rtype = sys.argv[sys.argv.index("--generate") + 1] if "--generate" in sys.argv and len(sys.argv) > sys.argv.index("--generate") + 1 else "pre_market"
        gen_map = {
            "pre_market": store.generate_pre_market,
            "morning_close": store.generate_morning_close,
            "midday_preview": store.generate_midday_preview,
            "full_day_review": store.generate_full_day_review,
            "weekly_summary": store.generate_weekly_summary,
            "signal_review": store.generate_signal_review,
        }
        if rtype in gen_map:
            r = gen_map[rtype](force=True)
            print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
        else:
            print(f"未知报告类型: {rtype}")
    elif "--list" in sys.argv:
        date_str = sys.argv[sys.argv.index("--list") + 1] if "--list" in sys.argv and len(sys.argv) > sys.argv.index("--list") + 1 else None
        reports = store.get_by_date(date_str)
        for r in reports:
            print(f"[{r['report_type']}] {r['title']} - {r['generated_at']}")
    elif "--check" in sys.argv:
        result = check_and_generate()
        print(f"生成报告: {result if result else '无需生成'}")
    else:
        print("用法: python report_store.py --generate <type> | --list [date] | --check")
