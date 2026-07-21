#!/usr/bin/env python3
"""
核心纯函数单元测试（零 I/O、无网络依赖）。

覆盖最核心、最易回归的纯逻辑：
  - 选股评分（min-max 归一化、智能偏离、资金流置信度）
  - 盯盘点位解析与信号映射
  - 选股退潮柔性降权（v4.6 增强）
  - 决策闭环胜率统计（P0-1 修复的 SQL）

运行:
  cd board_flow_dashboard && python3 -m unittest test_core.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


class TestScoring(unittest.TestCase):
    """选股评分纯函数。"""

    def setUp(self):
        import stock_selector as ss
        self.ss = ss

    def test_minmax_normalize(self):
        r = self.ss._minmax_normalize([1, 2, 3])
        self.assertEqual(r[1], 0.0)
        self.assertEqual(r[2], 0.5)
        self.assertEqual(r[3], 1.0)

    def test_minmax_all_equal(self):
        # 全相等不应除零崩溃
        r = self.ss._minmax_normalize([5, 5, 5])
        self.assertTrue(all(0 <= v <= 1 for v in r.values()))

    def test_smart_deviation_strong_inflow_outperform(self):
        # 强主力流入 + 跑赢板块 → 强势龙头，高分
        score = self.ss._smart_deviation_score(10, 5)
        self.assertGreaterEqual(score, 0.75)

    def test_smart_deviation_outflow_underperform(self):
        # 主力流出 + 跑输板块 → 真弱势，最低分
        score = self.ss._smart_deviation_score(-5, -5)
        self.assertLessEqual(score, 0.1)

    def test_flow_confidence_aligned_vs_diverged(self):
        # 价格与资金同向应比背离更可信
        aligned = self.ss._flow_confidence(10, 5)
        diverged = self.ss._flow_confidence(10, -5)
        self.assertGreaterEqual(aligned, diverged)


class TestSectorFade(unittest.TestCase):
    """v4.6 板块退潮柔性降权。"""

    def setUp(self):
        import stock_selector as ss
        self.ss = ss

    def test_fade_penalty_applied(self):
        stocks = [{"code": "A", "sector": "白酒", "score": 1.0}]
        self.ss._apply_sector_fade_penalty(stocks, {"白酒"}, set())
        self.assertAlmostEqual(stocks[0]["score"], self.ss.SECTOR_FADE_PENALTY)
        self.assertTrue(stocks[0]["fade_penalty"])

    def test_fade_skips_meltdown(self):
        # 已被硬熔断的板块不重复降权
        stocks = [{"code": "C", "sector": "稀土", "score": 1.0}]
        self.ss._apply_sector_fade_penalty(stocks, {"稀土"}, {"稀土"})
        self.assertEqual(stocks[0]["score"], 1.0)

    def test_fade_untouched_sector(self):
        stocks = [{"code": "B", "sector": "军工", "score": 1.0}]
        self.ss._apply_sector_fade_penalty(stocks, {"白酒"}, set())
        self.assertEqual(stocks[0]["score"], 1.0)


class TestWatcherPoints(unittest.TestCase):
    """持仓盯盘点位解析与信号映射。"""

    def setUp(self):
        import position_watcher as pw
        self.pw = pw

    def test_parse_pct_range(self):
        self.assertEqual(self.pw._parse_pct_range("4-6%"), 6.0)
        self.assertEqual(self.pw._parse_pct_range("5%"), 5.0)
        self.assertEqual(self.pw._parse_pct_range("-3%"), -3.0)
        self.assertEqual(self.pw._parse_pct_range("7-9%"), 9.0)
        self.assertIsNone(self.pw._parse_pct_range("-"))
        self.assertIsNone(self.pw._parse_pct_range(""))

    def test_default_stops_known_signal(self):
        tp, sl = self.pw._default_stops_for_signal("放量上攻")
        self.assertEqual(tp, 6.0)
        self.assertEqual(sl, -3.0)

    def test_default_stops_unknown_signal(self):
        tp, sl = self.pw._default_stops_for_signal("不存在的信号")
        self.assertEqual(tp, self.pw.DEFAULT_TAKE_PROFIT_PCT)
        self.assertEqual(sl, self.pw.DEFAULT_STOP_LOSS_PCT)


class TestWatcherFlow(unittest.TestCase):
    """盯盘录入/点位/触发全流程（临时 DB）。"""

    def setUp(self):
        import position_watcher as pw
        self.pw = pw
        pw.DB_PATH = Path(tempfile.mkdtemp()) / "test_watch.db"
        self.w = pw.PositionWatcher()

    def test_add_auto_points(self):
        r = self.w.add_position(code="600519", name="茅台", cost=100,
                                signal="放量上攻")
        self.assertTrue(r["ok"])
        self.assertEqual(r["target_price"], 106.0)  # +6%
        self.assertEqual(r["stop_price"], 97.0)      # -3%

    def test_add_rejects_bad_code(self):
        r = self.w.add_position(code="ABC", cost=100)
        self.assertFalse(r["ok"])

    def test_manual_override(self):
        r = self.w.add_position(code="600519", cost=100, signal="放量上攻")
        u = self.w.update_stops(r["id"], take_profit_pct=10.0)
        self.assertTrue(u["ok"])
        self.assertEqual(self.w.get_positions()[0]["target_price"], 110.0)

    def test_evaluate_triggers(self):
        fired = []
        self.w._push_alert = lambda *a, **k: fired.append(a[3])  # kind
        self.w._record_to_loop = lambda *a, **k: None  # 隔离闭环写入
        r = self.w.add_position(code="600519", cost=100, signal="放量上攻")
        pos = self.w.get_positions()[0]
        self.w._evaluate(pos, 107.0); self.w._alert_cooldown.clear()  # 止盈
        self.w._evaluate(pos, 96.5);  self.w._alert_cooldown.clear()  # 止损
        self.w._evaluate(pos, 97.6);  self.w._alert_cooldown.clear()  # 预警
        self.w._evaluate(pos, 101.0)                                  # 静默
        self.assertEqual(fired, ["take_profit", "stop_loss", "stop_warn"])

    def test_cooldown_dedup(self):
        self.assertTrue(self.w._cooldown_ok("600519", "stop_loss"))
        self.assertFalse(self.w._cooldown_ok("600519", "stop_loss"))


class TestDecisionLoop(unittest.TestCase):
    """决策闭环胜率统计（P0-1 修复的 SQL）。"""

    def setUp(self):
        import decision_store as ds
        self.ds = ds
        d = Path(tempfile.mkdtemp())
        ds.DATA_DIR = d
        ds.DB_PATH = d / "dec.db"
        ds._store = None
        self.store = ds.DecisionStore()

    def test_win_rate_stats_no_crash(self):
        # P0-1 回归测试：曾因 SQL 缺 END 崩溃
        for px, ex in [(100, 106), (100, 97)]:
            did = self.store.record_decision("600519", "茅台", px,
                                             signal="放量上攻", pool="A")
            self.store.mark_exited(did, ex)
        rates = self.store.get_signal_win_rates()
        self.assertTrue(rates)
        self.assertEqual(rates[0]["total_trades"], 2)
        self.assertEqual(rates[0]["win_rate"], 50.0)

    def test_total_stats(self):
        did = self.store.record_decision("600519", "茅台", 100, signal="X", pool="A")
        self.store.mark_exited(did, 110)
        s = self.store.get_total_stats()
        self.assertEqual(s["total_closed"], 1)
        self.assertEqual(s["win_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
