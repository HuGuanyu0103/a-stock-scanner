#!/usr/bin/env python3
"""
Loop Analyzer — 决策反馈闭环分析引擎

从 DecisionStore 读取历史决策数据，生成可注入 Agent 的反馈上下文。

核心输出:
  get_loop_context(): 反馈上下文字符串 → 注入 Agent System Prompt
  get_signal_hotness(): 信号热力 → 前端展示
  get_factor_performance(): 因子表现 → 权重调参建议

用法:
  from loop_analyzer import LoopAnalyzer
  la = LoopAnalyzer()
  context = la.get_loop_context()  # 注入 Agent
"""

from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class LoopAnalyzer:
    """反馈闭环分析器。

    不直接操作数据库，通过 DecisionStore 读取。分析结果以字典形式返回，
    由调用方决定如何注入 Agent Prompt 或展示到前端。
    """

    def __init__(self, store=None):
        if store is None:
            from decision_store import get_decision_store
            store = get_decision_store()
        self._store = store

    # ── Agent 反馈上下文 ──────────────────────────────────────

    def get_loop_context(self) -> str:
        """生成可注入 Agent 的反馈上下文。

        当历史数据充足时，Agent 可以引用：
        - 「放量上攻」信号历史胜率 68%
        - 当前已有 3 只持仓，建议控制仓位
        """
        return self._store.get_loop_context()

    def get_agent_context_injection(self) -> dict:
        """生成结构化反馈数据，供 Agent System Prompt 追加。

        Returns:
            {"has_history": bool, "context_text": str, "top_signals": [...]}
        """
        stats = self._store.get_total_stats()
        signal_rates = self._store.get_signal_win_rates()
        open_decisions = self._store.get_open_decisions()

        has_history = stats["total_closed"] >= 5

        if not has_history:
            return {
                "has_history": False,
                "context_text": "",
                "top_signals": [],
                "open_count": len(open_decisions),
            }

        # 构建简洁的注入文本
        lines = [
            f"[反馈数据] 历史{stats['total_closed']}笔交易, "
            f"胜率{stats['win_rate']}%, 均收益{stats['avg_return']:+.2f}%",
        ]

        if signal_rates:
            top = [f"{s['signal_combo']}({s['win_rate']}%/{s['total_trades']}笔)"
                   for s in signal_rates[:5]]
            lines.append(f"高胜率信号: {', '.join(top)}")

        if open_decisions:
            lines.append(f"当前持仓{len(open_decisions)}只, 注意仓位管理")

        return {
            "has_history": True,
            "context_text": " | ".join(lines),
            "top_signals": signal_rates[:8],
            "open_count": len(open_decisions),
            "stats": stats,
        }

    # ── 信号热力分析 ──────────────────────────────────────────

    def get_signal_hotness(self) -> list[dict]:
        """各信号近期的热度（最近 N 次交易中出现的频率和胜率）。

        用于前端展示「当前哪些信号正在赚钱」。
        """
        decisions = self._store.get_recent_decisions(limit=50)
        if not decisions:
            return []

        # 按信号聚合
        signal_map: dict[str, dict] = {}
        for d in decisions:
            sig = d.get("signal", "") or "未分类"
            if sig not in signal_map:
                signal_map[sig] = {"signal": sig, "count": 0, "wins": 0, "returns": []}
            signal_map[sig]["count"] += 1
            rp = d.get("return_pct")
            if rp is not None:
                signal_map[sig]["returns"].append(rp)
                if rp > 0:
                    signal_map[sig]["wins"] += 1

        # 计算胜率和平均收益
        result = []
        for sig, data in signal_map.items():
            n = data["count"]
            w = data["wins"]
            rets = data["returns"]
            result.append({
                "signal": sig,
                "total": n,
                "win_rate": round(w / n * 100, 1) if n > 0 else 0,
                "avg_return": round(sum(rets) / len(rets), 2) if rets else 0,
                "hot": n >= 5 and w / n > 0.5,  # 热信号 = 出现频繁且胜率>50%
                "cold": n >= 3 and w / n < 0.35,  # 冷信号 = 胜率<35%
            })

        result.sort(key=lambda x: -x["total"])
        return result

    # ── 因子权重建议 ──────────────────────────────────────────

    def get_factor_weight_advice(self) -> dict:
        """基于历史数据，给出因子权重的调整建议。

        分析维度：
        - 量比 vs 胜率的相关性
        - 主力净占比 vs 胜率
        - 日内位置最优区间

        Returns:
            {"advice": str, "details": {...}}
        """
        decisions = [d for d in self._store.get_recent_decisions(limit=100)
                     if d.get("return_pct") is not None]

        if len(decisions) < 10:
            return {"advice": "数据不足（需10笔以上结算记录）", "details": {}}

        # 按收益分组
        wins = [d for d in decisions if d["return_pct"] > 0]
        losses = [d for d in decisions if d["return_pct"] <= 0]

        # 分析信号与胜率的关系
        win_signals = {}
        for d in wins:
            sig = d.get("signal", "?")
            win_signals[sig] = win_signals.get(sig, 0) + 1

        # 生成建议
        advice_parts = []
        if losses:
            loss_signals = {}
            for d in losses:
                loss_signals[d.get("signal", "?")] = loss_signals.get(d.get("signal", "?"), 0) + 1
            # 找出亏损最频繁的信号
            worst = sorted(loss_signals.items(), key=lambda x: -x[1])[:3]
            if worst and worst[0][1] >= 3:
                worst_names = [w[0] for w in worst]
                advice_parts.append(f"警惕信号: {', '.join(worst_names)}")

        if wins and len(wins) > len(losses):
            advice_parts.append(f"近期胜率较高({len(wins)}/{len(decisions)})，可适度积极")
        elif losses and len(losses) > len(wins):
            advice_parts.append(f"近期胜率偏低({len(wins)}/{len(decisions)})，建议减仓观望")

        return {
            "advice": "；".join(advice_parts) if advice_parts else "数据不足，暂无建议",
            "details": {
                "total_analyzed": len(decisions),
                "recent_win_rate": round(len(wins) / len(decisions) * 100, 1),
                "top_win_signals": sorted(win_signals.items(), key=lambda x: -x[1])[:5],
            },
        }


# ── 全局单例 ──────────────────────────────────────────────────

_analyzer: Optional[LoopAnalyzer] = None


def get_loop_analyzer() -> LoopAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = LoopAnalyzer()
    return _analyzer
