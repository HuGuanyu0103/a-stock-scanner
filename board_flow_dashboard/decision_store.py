#!/usr/bin/env python3
"""决策日志存储 — Loop Engineering + 影子模式 + 基准对照"""
from __future__ import annotations
import json, logging, os, sqlite3, threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "decisions.db"
HOLDING_DAYS = 3

class DecisionStore:
    def __init__(self):
        self._lock = threading.Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT NOT NULL, stock_name TEXT NOT NULL,
                    sector TEXT DEFAULT '', pool TEXT DEFAULT '', signal TEXT DEFAULT '',
                    entry_price REAL NOT NULL, entry_date TEXT NOT NULL,
                    exit_price REAL, exit_date TEXT, return_pct REAL,
                    status TEXT DEFAULT 'open', source TEXT DEFAULT 'agent',
                    confidence INTEGER DEFAULT 3, score REAL DEFAULT 0,
                    signal_combo TEXT DEFAULT '', notes TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS signal_combo_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, signal_combo TEXT UNIQUE NOT NULL,
                    total_trades INTEGER DEFAULT 0, win_trades INTEGER DEFAULT 0,
                    avg_return REAL DEFAULT 0, last_updated TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT NOT NULL, stock_name TEXT NOT NULL,
                    sector TEXT DEFAULT '', pool TEXT DEFAULT '', signal TEXT DEFAULT '',
                    recommend_price REAL, entry_date TEXT NOT NULL,
                    exit_price REAL, exit_date TEXT, return_pct REAL,
                    status TEXT DEFAULT 'tracking', adopted INTEGER DEFAULT 0,
                    signal_combo TEXT DEFAULT '', agent_confidence INTEGER DEFAULT 3,
                    agent_score REAL DEFAULT 0, source TEXT DEFAULT 'agent',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS benchmark_index (
                    trade_date TEXT PRIMARY KEY, csi300_return REAL
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
                CREATE INDEX IF NOT EXISTS idx_decisions_entry ON decisions(entry_date);
                CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_decisions(status);
                CREATE INDEX IF NOT EXISTS idx_shadow_entry ON shadow_decisions(entry_date);
            """)

    # ── 写入 ─────────────────────────────────────────────────
    def record_decision(self, stock_code, stock_name, entry_price, sector="", pool="", signal="", confidence=3, score=0, source="agent", signal_combo="", notes=""):
        today = date.today().strftime("%Y-%m-%d")
        if not signal_combo and signal: signal_combo = f"{pool}-{signal}"
        with self._lock:
            with self._get_conn() as conn:
                c = conn.execute("INSERT INTO decisions (stock_code,stock_name,sector,pool,signal,entry_price,entry_date,status,source,confidence,score,signal_combo,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))", (stock_code,stock_name,sector,pool,signal,entry_price,today,"open",source,confidence,score,signal_combo,notes))
                return c.lastrowid

    def mark_exited(self, decision_id, exit_price, exit_date="", notes=""):
        exit_date = exit_date or date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("SELECT entry_price FROM decisions WHERE id=?",(decision_id,)).fetchone()
                if not row: return
                rp = round((exit_price-row["entry_price"])/row["entry_price"]*100,2)
                conn.execute("UPDATE decisions SET exit_price=?,exit_date=?,return_pct=?,status='closed',notes=notes||? WHERE id=?",(exit_price,exit_date,rp,f" exit @{exit_price}",decision_id))

    def auto_resolve(self, days_threshold=HOLDING_DAYS):
        cutoff = (date.today()-timedelta(days=days_threshold)).strftime("%Y-%m-%d")
        resolved = 0
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT id,stock_code,entry_price FROM decisions WHERE status='open' AND entry_date<=?",(cutoff,)).fetchall()
        for d in rows:
            try:
                p = self._fetch_latest_close(d["stock_code"])
                if p is None: continue
                rp = round((p-d["entry_price"])/d["entry_price"]*100,2)
                with self._lock:
                    with self._get_conn() as conn:
                        conn.execute("UPDATE decisions SET exit_price=?,exit_date=?,return_pct=?,status='closed' WHERE id=?",(p,date.today().strftime("%Y-%m-%d"),rp,d["id"]))
                resolved += 1
            except Exception: pass
        if resolved: self._refresh_combo_stats()
        return resolved

    def _fetch_latest_close(self, stock_code):
        try:
            import akshare as ak
            m = "sh" if stock_code.startswith(("6","9")) else "sz"
            df = ak.stock_zh_a_hist(symbol=f"{m}{stock_code}",period="daily",start_date=(date.today()-timedelta(days=5)).strftime("%Y%m%d"),end_date=date.today().strftime("%Y%m%d"),adjust="qfq")
            if df is not None and not df.empty: return float(df.iloc[-1]["收盘"])
        except Exception: pass
        try:
            import requests as req
            r = req.get("https://push2.eastmoney.com/api/qt/stock/get",params={"secid":f"{'1' if stock_code.startswith(('6','9')) else '0'}.{stock_code}","fields":"f43"},timeout=5)
            p = r.json().get("data",{}).get("f43")
            if p: return float(p)/100
        except Exception: pass
        return None

    # ── 基准指数对照 ───────────────────────────────────────
    def record_benchmark(self, trade_date="", csi300_return=0):
        trade_date = trade_date or date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("INSERT OR REPLACE INTO benchmark_index VALUES (?,?)",(trade_date,csi300_return))

    def get_excess_return(self):
        s = self.get_total_stats()
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("SELECT AVG(return_pct) as a, AVG(return_pct)-(SELECT AVG(csi300_return)*100 FROM benchmark_index) as e FROM decisions WHERE status='closed'").fetchone()
        return {"avg_return":round(row["a"],2) if row and row["a"] else 0,"excess_return":round(row["e"],2) if row and row["e"] else 0,"total_closed":s["total_closed"]}

    # ── 影子模式 ───────────────────────────────────────────
    def record_shadow_batch(self, picks):
        today = date.today().strftime("%Y-%m-%d")
        n = 0
        with self._lock:
            with self._get_conn() as conn:
                for p in picks:
                    conn.execute("INSERT INTO shadow_decisions (stock_code,stock_name,sector,pool,signal,recommend_price,entry_date,status,adopted,signal_combo,agent_confidence,agent_score,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",(p.get("code",""),p.get("name",""),p.get("sector",""),p.get("pool",""),p.get("signal",""),p.get("price",0),today,"tracking",0,p.get("pool","")+"-"+p.get("signal",""),p.get("confidence",3),p.get("score",0),"agent_shadow"))
                    n += 1
        return n

    def auto_resolve_shadows(self, days_threshold=3):
        cutoff = (date.today()-timedelta(days=days_threshold)).strftime("%Y-%m-%d")
        r = 0
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT id,stock_code FROM shadow_decisions WHERE status='tracking' AND entry_date<=?",(cutoff,)).fetchall()
        for d in rows:
            try:
                p = self._fetch_latest_close(d["stock_code"])
                if p is None: continue
                with self._lock:
                    with self._get_conn() as conn:
                        conn.execute("UPDATE shadow_decisions SET exit_price=?,exit_date=?,status='resolved' WHERE id=?",(p,date.today().strftime("%Y-%m-%d"),d["id"]))
                r += 1
            except Exception: pass
        return r

    def get_shadow_stats(self):
        with self._lock:
            with self._get_conn() as conn:
                aa = conn.execute("SELECT AVG(return_pct) as v, COUNT(*) as n FROM shadow_decisions WHERE status='resolved'").fetchone()
                ad = conn.execute("SELECT AVG(return_pct) as v, COUNT(*) as n FROM shadow_decisions WHERE status='resolved' AND adopted=1").fetchone()
        return {"all_avg_return":round(aa["v"],2) if aa and aa["v"] else 0,"all_count":aa["n"] if aa else 0,"adopted_avg_return":round(ad["v"],2) if ad and ad["v"] else 0,"adopted_count":ad["n"] if ad else 0,"selection_bias":round((ad["v"] or 0)-(aa["v"] or 0),2) if aa and ad else 0}

    # ── 查询 ─────────────────────────────────────────────────
    def get_open_decisions(self):
        with self._lock:
            with self._get_conn() as conn:
                return [dict(r) for r in conn.execute("SELECT * FROM decisions WHERE status='open' ORDER BY created_at DESC").fetchall()]

    def get_recent_decisions(self, limit=20):
        with self._lock:
            with self._get_conn() as conn:
                return [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]

    def get_total_stats(self):
        with self._lock:
            with self._get_conn() as conn:
                t = conn.execute("SELECT COUNT(*) as n FROM decisions WHERE status='closed'").fetchone()["n"]
                w = conn.execute("SELECT COUNT(*) as n FROM decisions WHERE status='closed' AND return_pct>0").fetchone()["n"]
                a = conn.execute("SELECT AVG(return_pct) as v FROM decisions WHERE status='closed'").fetchone()["v"]
        return {"total_closed":t,"win_count":w,"loss_count":t-w,"win_rate":round(w/t*100,1) if t>0 else 0,"avg_return":round(a,2) if a else 0,"open_count":self._count_open()}

    def _count_open(self):
        with self._lock:
            with self._get_conn() as conn:
                return conn.execute("SELECT COUNT(*) as n FROM decisions WHERE status='open'").fetchone()["n"]

    def get_signal_win_rates(self):
        self._refresh_combo_stats()
        with self._lock:
            with self._get_conn() as conn:
                return [dict(r) for r in conn.execute("SELECT signal_combo,total_trades,win_trades,ROUND(win_trades*100.0/total_trades,1) as win_rate,ROUND(avg_return,2) as avg_ret FROM signal_combo_stats WHERE total_trades>=2 ORDER BY total_trades DESC LIMIT 20").fetchall()]

    def get_loop_context(self):
        s = self.get_total_stats()
        sr = self.get_signal_win_rates()
        od = self.get_open_decisions()
        lines = [f"=== 历史决策反馈数据 ===",f"累计结算: {s['total_closed']} 笔",f"胜率: {s['win_rate']}%",f"平均收益: {s['avg_return']:+.2f}%"]
        if sr:
            lines.append("\n信号组合历史胜率:")
            for x in sr[:8]: lines.append(f"  {x['signal_combo']}: {x['total_trades']}笔 胜率{x['win_rate']}% 均收益{x['avg_ret']:+.2f}%")
        if od:
            lines.append(f"\n当前持仓: {len(od)} 只")
            for d in od[:5]:
                dh = (date.today()-datetime.strptime(d["entry_date"],"%Y-%m-%d").date()).days
                lines.append(f"  {d['stock_name']} {d['stock_code']} 入场{d['entry_price']:.2f} 持有{dh}天")
        return "\n".join(lines)

    def _refresh_combo_stats(self):
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM signal_combo_stats")
                conn.execute("INSERT INTO signal_combo_stats (signal_combo,total_trades,win_trades,avg_return) SELECT signal_combo,COUNT(*) as t,SUM(CASE WHEN return_pct>0 THEN 1 ELSE 0 END) as w,AVG(return_pct) as a FROM decisions WHERE status='closed' AND signal_combo!='' GROUP BY signal_combo HAVING t>=2")

_store: Optional[DecisionStore] = None
def get_decision_store() -> DecisionStore:
    global _store
    if _store is None: _store = DecisionStore()
    return _store
