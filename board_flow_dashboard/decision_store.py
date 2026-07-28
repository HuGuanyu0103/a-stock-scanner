#!/usr/bin/env python3
"""决策日志存储 — Loop Engineering + 影子模式 + 基准对照"""
from __future__ import annotations
import json, logging, os, sqlite3, threading, time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "decisions.db"
HOLDING_DAYS = 3

# ── L3 结算严谨化参数 ──────────────────────────────────────────
TRADING_COST_PCT = 0.1     # 双边交易成本（买卖佣金+印花税+过户费，约 0.1%），从收益里扣
LIMIT_UP_PCT = 9.8         # 主板涨停阈值（当日涨幅≥此值视为涨停，卖出可能无法成交）
LIMIT_DOWN_PCT = -9.8      # 主板跌停阈值
SETTLE_MAX_DEFER_DAYS = 4  # 停牌顺延最多再等 N 个自然日，超过则按最新可得价强制结算

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
                did = c.lastrowid
        # L5: 用户采纳某票时，把当日对应的影子记录标记 adopted=1，
        # 让 get_shadow_stats 的 selection_bias(采纳票收益 - 全体影子收益) 有意义
        self.mark_adopted(stock_code)
        return did

    def mark_adopted(self, stock_code, entry_date=""):
        """把指定股票当日(或指定日)的影子记录标记为已采纳。

        修复 adopted 字段永远为 0 的断点——selection_bias 度量依赖它。
        """
        entry_date = entry_date or date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE shadow_decisions SET adopted=1 WHERE stock_code=? AND entry_date=?",
                    (stock_code, entry_date))

    def mark_exited(self, decision_id, exit_price, exit_date="", notes=""):
        exit_date = exit_date or date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("SELECT entry_price FROM decisions WHERE id=?",(decision_id,)).fetchone()
                if not row: return
                rp = round((exit_price-row["entry_price"])/row["entry_price"]*100,2)
                # 修复：原实现 notes=notes||? 中 notes 指列名、入参 notes 从未被用；
                # 且列为 NULL 时 ||结果为 NULL。改为 COALESCE(列,'') 追加，且带上入参 notes。
                suffix = (f" {notes}" if notes else "") + f" exit @{exit_price}"
                conn.execute("UPDATE decisions SET exit_price=?,exit_date=?,return_pct=?,status='closed',notes=COALESCE(notes,'')||? WHERE id=?",(exit_price,exit_date,rp,suffix,decision_id))
        if True:
            self._refresh_combo_stats()

    def auto_resolve(self, days_threshold=HOLDING_DAYS):
        # L3: 用交易日而非自然日。N 交易日≈ceil(N*7/5) 自然日 + 2 缓冲，覆盖周末。
        # entry_date 早于该自然日 cutoff 的才够 N 个交易日持有期。
        import math
        natural_days = math.ceil(days_threshold * 7 / 5) + 1
        cutoff = (date.today() - timedelta(days=natural_days)).strftime("%Y-%m-%d")
        resolved, deferred = 0, 0
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT id,stock_code,entry_price,entry_date FROM decisions WHERE status='open' AND entry_date<=?", (cutoff,)).fetchall()
        for d in rows:
            try:
                sd = self._fetch_settle_data(d["stock_code"])
                if sd is None:
                    # 取数失败 → 顺延，不强行结算
                    deferred += 1
                    continue
                # L3: 停牌不结算，顺延（但超过最大顺延期则强制结算，避免永久滞留）
                held_days = (date.today() - datetime.strptime(d["entry_date"], "%Y-%m-%d").date()).days
                if sd["halted"] and held_days <= natural_days + SETTLE_MAX_DEFER_DAYS:
                    deferred += 1
                    continue
                exit_price = sd["close"]
                gross = (exit_price - d["entry_price"]) / d["entry_price"] * 100
                # L3: 扣双边交易成本
                net = gross - TRADING_COST_PCT
                # L3: 涨跌停标记（卖出可能无法成交，收益仅为账面参考）
                note_flag = ""
                if sd["pct_chg"] >= LIMIT_UP_PCT:
                    note_flag = " [涨停/卖出受限,账面价]"
                elif sd["pct_chg"] <= LIMIT_DOWN_PCT:
                    note_flag = " [跌停/卖出受限,账面价]"
                rp = round(net, 2)
                with self._lock:
                    with self._get_conn() as conn:
                        conn.execute(
                            "UPDATE decisions SET exit_price=?,exit_date=?,return_pct=?,status='closed',"
                            "notes=COALESCE(notes,'')||? WHERE id=?",
                            (exit_price, date.today().strftime("%Y-%m-%d"), rp,
                             f" settle net@{exit_price}{note_flag}", d["id"]))
                resolved += 1
            except Exception:
                pass
        if resolved:
            self._refresh_combo_stats()
        if deferred:
            logger.info("结算顺延 %d 笔(停牌/取数失败)", deferred)
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

    def _fetch_settle_data(self, stock_code):
        """L3: 结算专用取数，返回 {close, pct_chg, volume, halted}。

        比 _fetch_latest_close 多返回当日涨跌幅（判涨跌停）和成交量（判停牌）。
        取最近一个交易日的日线。失败返回 None（调用方顺延）。
        """
        try:
            import akshare as ak
            m = "sh" if stock_code.startswith(("6", "9")) else "sz"
            df = ak.stock_zh_a_hist(
                symbol=f"{m}{stock_code}", period="daily",
                start_date=(date.today() - timedelta(days=8)).strftime("%Y%m%d"),
                end_date=date.today().strftime("%Y%m%d"), adjust="qfq")
            if df is None or df.empty:
                return None
            last = df.iloc[-1]
            close = float(last["收盘"])
            pct = float(last["涨跌幅"]) if "涨跌幅" in df.columns else 0.0
            vol = float(last["成交量"]) if "成交量" in df.columns else 0.0
            return {"close": close, "pct_chg": pct, "volume": vol,
                    "halted": vol <= 0}  # 成交量为 0 视为停牌
        except Exception:
            return None

    # ── 基准指数对照 ───────────────────────────────────────
    def record_benchmark(self, trade_date="", csi300_return=0):
        trade_date = trade_date or date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("INSERT OR REPLACE INTO benchmark_index VALUES (?,?)",(trade_date,csi300_return))

    def get_excess_return(self):
        """超额收益（逐笔对齐同期基准）。

        修正前实现是「AVG(所有决策3日累计收益) - AVG(benchmark所有记录日单日均值)」，
        两个时间窗口完全不对齐（持有期累计 vs 单日均值），相减无意义。
        现改为：每笔已结算决策，取其 entry_date→exit_date 区间内沪深300日涨跌幅累乘得到
        同期基准区间收益，逐笔算 alpha = 个股收益 - 同期基准收益，再对所有笔求平均。
        """
        with self._lock:
            with self._get_conn() as conn:
                decs = conn.execute(
                    "SELECT return_pct, entry_date, exit_date FROM decisions "
                    "WHERE status='closed' AND return_pct IS NOT NULL AND exit_date IS NOT NULL"
                ).fetchall()
                bench_rows = conn.execute(
                    "SELECT trade_date, csi300_return FROM benchmark_index ORDER BY trade_date"
                ).fetchall()

        if not decs:
            return {"avg_return": 0, "excess_return": 0, "total_closed": 0, "matched": 0}

        # 基准日涨跌幅（小数）按日期索引
        bench = {r["trade_date"]: (r["csi300_return"] or 0) for r in bench_rows}

        alphas, rets = [], []
        matched = 0
        for d in decs:
            ret = d["return_pct"]  # 百分数，如 +3.5
            rets.append(ret)
            ed, xd = d["entry_date"], d["exit_date"]
            # 累乘 (entry, exit] 区间内的基准日涨跌幅 → 区间基准收益（百分数）
            period = [v for dt, v in bench.items() if ed < dt <= xd]
            if not period:
                continue  # 无同期基准数据的笔不计入 alpha（但仍计入 avg_return）
            comp = 1.0
            for v in period:
                comp *= (1 + v)
            bench_ret = (comp - 1) * 100  # 转百分数
            alphas.append(ret - bench_ret)
            matched += 1

        avg_return = round(sum(rets) / len(rets), 2) if rets else 0
        excess = round(sum(alphas) / len(alphas), 2) if alphas else 0
        return {"avg_return": avg_return, "excess_return": excess,
                "total_closed": len(decs), "matched": matched}

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

    def record_shadow_batch_daily(self, picks):
        """当日去重版影子记录：同一交易日同一 code 只记一次。

        供选股 Agent 推荐时调用——每次推荐可能重复，靠 (code, entry_date)
        去重避免刷量，让影子样本干净可用于「选择偏差」度量。
        """
        today = date.today().strftime("%Y-%m-%d")
        fresh = []
        with self._lock:
            with self._get_conn() as conn:
                exist = {r["stock_code"] for r in conn.execute(
                    "SELECT stock_code FROM shadow_decisions WHERE entry_date=?",
                    (today,)).fetchall()}
        for p in picks:
            if p.get("code") and p["code"] not in exist:
                fresh.append(p)
        return self.record_shadow_batch(fresh) if fresh else 0

    def auto_resolve_shadows(self, days_threshold=3):
        # L3: 交易日阈值，与实盘 auto_resolve 口径一致
        import math
        natural_days = math.ceil(days_threshold * 7 / 5) + 1
        cutoff = (date.today()-timedelta(days=natural_days)).strftime("%Y-%m-%d")
        r = 0
        with self._lock:
            with self._get_conn() as conn:
                # 取 recommend_price 作为入场价，用于结算时计算 return_pct
                rows = conn.execute("SELECT id,stock_code,recommend_price FROM shadow_decisions WHERE status='tracking' AND entry_date<=?",(cutoff,)).fetchall()
        for d in rows:
            try:
                sd = self._fetch_settle_data(d["stock_code"])
                if sd is None or sd["halted"]:
                    continue  # 取数失败/停牌 → 顺延
                p = sd["close"]
                # 修复：结算影子决策时补算 return_pct（原实现只写 exit_price，导致胜率统计拿不到收益）
                # L3: 同样扣双边交易成本，与实盘口径一致
                rec = d["recommend_price"]
                rp = round((p-rec)/rec*100 - TRADING_COST_PCT, 2) if rec and rec > 0 else None
                with self._lock:
                    with self._get_conn() as conn:
                        conn.execute("UPDATE shadow_decisions SET exit_price=?,exit_date=?,return_pct=?,status='resolved' WHERE id=?",(p,date.today().strftime("%Y-%m-%d"),rp,d["id"]))
                r += 1
            except Exception: pass
        return r

    def get_shadow_stats(self):
        with self._lock:
            with self._get_conn() as conn:
                aa = conn.execute("SELECT AVG(return_pct) as v, COUNT(*) as n FROM shadow_decisions WHERE status='resolved'").fetchone()
                ad = conn.execute("SELECT AVG(return_pct) as v, COUNT(*) as n FROM shadow_decisions WHERE status='resolved' AND adopted=1").fetchone()
        return {"all_avg_return":round(aa["v"],2) if aa and aa["v"] else 0,"all_count":aa["n"] if aa else 0,"adopted_avg_return":round(ad["v"],2) if ad and ad["v"] else 0,"adopted_count":ad["n"] if ad else 0,"selection_bias":round((ad["v"] or 0)-(aa["v"] or 0),2) if aa and ad else 0}

    def get_source_win_rates(self):
        """M1: 按来源(source)分组的影子胜率对比 —— 回答"辩论选的票 vs 单Agent vs 规则,谁更准"。

        source 取值：agent_shadow(单Agent选股) / debate(多Agent辩论共识) / agent(其他)。
        这是多智能体到底值不值的实证出口。
        """
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute("""
                    SELECT source,
                           COUNT(*) as n,
                           SUM(CASE WHEN return_pct>0 THEN 1 ELSE 0 END) as w,
                           ROUND(AVG(return_pct),2) as avg_ret
                    FROM shadow_decisions
                    WHERE status='resolved' AND return_pct IS NOT NULL
                    GROUP BY source ORDER BY n DESC
                """).fetchall()
        out = []
        for r in rows:
            n = r["n"] or 0
            out.append({
                "source": r["source"] or "unknown",
                "count": n,
                "win_rate": round((r["w"] or 0) / n * 100, 1) if n else 0,
                "avg_return": r["avg_ret"] or 0,
            })
        return out

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
        # 飞轮修复：统计同时纳入实盘 decisions 与影子 shadow_decisions（已结算）。
        # 原实现只读 decisions，而 decisions 依赖用户手动采纳、常年为空，导致飞轮空转；
        # shadow_decisions 由 Agent 推荐自动写入+自动结算，是唯一稳定积累的数据源。
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM signal_combo_stats")
                conn.execute("""
                    INSERT INTO signal_combo_stats (signal_combo,total_trades,win_trades,avg_return)
                    SELECT signal_combo,
                           COUNT(*) as t,
                           SUM(CASE WHEN return_pct>0 THEN 1 ELSE 0 END) as w,
                           AVG(return_pct) as a
                    FROM (
                        SELECT signal_combo, return_pct FROM decisions
                            WHERE status='closed' AND signal_combo!='' AND return_pct IS NOT NULL
                        UNION ALL
                        SELECT signal_combo, return_pct FROM shadow_decisions
                            WHERE status='resolved' AND signal_combo!='' AND return_pct IS NOT NULL
                    )
                    GROUP BY signal_combo HAVING t>=2
                """)

    # ── P1-1: 自动结算调度（让决策飞轮自动运转）─────────────
    def start_auto_resolve(self, check_interval: float = 3600.0):
        """启动后台线程，每 check_interval 秒尝试自动结算到期的决策与影子决策。

        不依赖用户手动点按钮。auto_resolve 内部按持有天数阈值筛选，
        未到期的记录不受影响。收盘后与开盘前都会跑，保证每交易日至少结算一次。
        """
        import threading as _th
        if getattr(self, "_auto_thread", None) and self._auto_thread.is_alive():
            return

        def _loop():
            while getattr(self, "_auto_running", True):
                try:
                    n = self.auto_resolve()
                    m = self.auto_resolve_shadows()
                    if n or m:
                        logger.info("自动结算: 决策 %d 笔, 影子 %d 笔", n, m)
                    self._record_today_benchmark()
                except Exception as e:
                    logger.warning("自动结算异常: %s", e)
                time.sleep(check_interval)

        self._auto_running = True
        self._auto_thread = _th.Thread(target=_loop, daemon=True,
                                       name="decision-auto-resolve")
        self._auto_thread.start()
        logger.info("决策自动结算调度已启动 (间隔 %.0fs)", check_interval)

    def stop_auto_resolve(self):
        self._auto_running = False

    def _record_today_benchmark(self):
        """记录当日沪深300涨跌幅（供 get_excess_return 计算超额收益）。

        当日已记录则跳过；akshare 不可用/失败静默跳过，不影响结算主流程。
        存储量纲：小数（如 +1.5% 存 0.015），与 get_excess_return 的 *100 匹配。
        """
        today = date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self._get_conn() as conn:
                exist = conn.execute(
                    "SELECT 1 FROM benchmark_index WHERE trade_date=?",
                    (today,)).fetchone()
        if exist:
            return
        try:
            import akshare as ak
            df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
            row = df[df["代码"] == "000300"]
            if row is not None and not row.empty:
                pct = float(row.iloc[0]["涨跌幅"]) / 100.0  # 转小数
                self.record_benchmark(today, pct)
                logger.info("记录基准: 沪深300 %s %+.2f%%", today, pct * 100)
        except Exception as e:
            logger.debug("基准记录跳过: %s", e)


_store: Optional[DecisionStore] = None
def get_decision_store() -> DecisionStore:
    global _store
    if _store is None: _store = DecisionStore()
    return _store
