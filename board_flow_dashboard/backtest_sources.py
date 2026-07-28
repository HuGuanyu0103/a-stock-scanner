#!/usr/bin/env python3
"""
三源选股离线对比回测框架（Rule vs Single-Agent vs Multi-Agent Debate）

回答 AI 产品岗最想听的问题：「你怎么量化多智能体到底值不值？」——在同一候选池、
同一前向收益口径下，对照三种决策来源的胜率 / 平均收益 / 超额收益(alpha)。

━━ 诚信声明（重要）━━
本框架产出的数字有两种来源，输出里始终以 data_source 字段显式标注，绝不混淆：
  • "live"      —— 来自 decision_store 真实累积的已结算样本（系统跑够时间后才有）；
  • "SIMULATED" —— 无真实样本时，用固定随机种子生成的**模拟**数据集，仅用于验证
                    回测管线与口径是否正确，**不代表真实收益，不可写作实盘业绩**。
当前若数据库为空，只会得到 SIMULATED 结果——请勿将其当作真实战绩对外陈述。

口径（与 decision_store.get_excess_return 对齐）：
  单笔 alpha = 个股持有期收益 - 同期基准(沪深300)收益；胜率 = 收益>0 笔数 / 总笔数。

用法：
  python3 backtest_sources.py            # 自动选 live / 无则 SIMULATED
  python3 backtest_sources.py --sim 300  # 强制模拟 300 笔/源（可复现，种子固定）
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SOURCES = ["rule", "agent", "debate"]  # 规则 / 单Agent / 多Agent辩论
SOURCE_LABEL = {"rule": "规则引擎", "agent": "单Agent", "debate": "多Agent辩论"}


def _summarize(trades: list[dict]) -> dict:
    """把一组 {return_pct, alpha} 交易汇总为胜率/均收益/均alpha。"""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_return": 0.0, "avg_alpha": 0.0}
    wins = sum(1 for t in trades if (t.get("return_pct") or 0) > 0)
    avg_ret = sum(t.get("return_pct") or 0 for t in trades) / n
    avg_alpha = sum(t.get("alpha") or 0 for t in trades) / n
    return {"n": n, "win_rate": round(wins / n * 100, 1),
            "avg_return": round(avg_ret, 2), "avg_alpha": round(avg_alpha, 2)}


# ── 数据源 1：真实（decision_store 已结算 + 基准对齐）──────────────
def _load_live(store) -> dict[str, list]:
    """从 decision_store 拉真实已结算交易，按 source 分组并逐笔算 alpha。

    复用 get_settled_trades（含 signal_combo/return_pct/exit_date）；alpha 需基准，
    这里若无逐笔基准则以 0 兜底（口径退化为纯收益，输出会标注）。返回 {source: [trades]}。
    """
    try:
        trades = store.get_settled_trades()
    except Exception as e:
        logger.warning("读取真实交易失败: %s", e)
        return {}
    # get_settled_trades 未带 source，改用 source 级聚合接口交叉；此处退化为整体池
    # （真实 source 分组在 decision_store.get_source_win_rates 已有，直接引用其口径）
    try:
        rows = store.get_source_win_rates()  # [{source,count,win_rate,avg_return}]
    except Exception:
        rows = []
    return {"_source_rows": rows, "_total": len(trades)}


# ── 数据源 2：模拟（固定种子，可复现，仅验证管线）──────────────────
def _make_simulated(n_per_source: int = 200) -> dict[str, list]:
    """生成三源模拟交易。用线性同余自造伪随机（不依赖 random，保证跨环境可复现）。

    设定（仅为演示口径，非任何真实先验）：三源各有不同的"信号质量"，辩论>单Agent>规则，
    但都叠加较大市场噪声——以此检验回测能否在噪声中把差异算出来。
    """
    # 可复现 PRNG（线性同余），种子固定
    state = 20260728

    def rnd() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF  # [0,1)

    def gauss(mu, sigma):
        # Box-Muller（两个均匀数）
        import math
        u1, u2 = max(rnd(), 1e-9), rnd()
        z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
        return mu + sigma * z

    # 各源"真实"日均超额(pp)与噪声——辩论略优，但噪声大到不显眼，考验回测灵敏度
    profile = {
        "rule":   {"mu_alpha": 0.15, "sigma": 3.2},
        "agent":  {"mu_alpha": 0.45, "sigma": 3.0},
        "debate": {"mu_alpha": 0.80, "sigma": 2.8},
    }
    out: dict[str, list] = {}
    for src in SOURCES:
        p = profile[src]
        trades = []
        for _ in range(n_per_source):
            bench = gauss(0.05, 1.5)          # 同期基准收益(pp)
            alpha = gauss(p["mu_alpha"], p["sigma"])
            ret = round(bench + alpha, 2)      # 个股收益 = 基准 + alpha
            trades.append({"return_pct": ret, "alpha": round(alpha, 2)})
        out[src] = trades
    return out


def run_backtest(store=None, force_sim: Optional[int] = None) -> dict:
    """跑三源对比。优先真实数据；无则模拟（显式标注）。返回结构化报告。"""
    report = {"sources": {}, "ranking": [], "note": ""}

    # 1) 决定数据源
    live_rows = []
    total_live = 0
    if force_sim is None and store is not None:
        live = _load_live(store)
        live_rows = live.get("_source_rows", []) or []
        total_live = live.get("_total", 0) or 0

    if live_rows and total_live > 0:
        report["data_source"] = "live"
        report["note"] = "来自决策飞轮真实已结算样本"
        for r in live_rows:
            src = r.get("source", "")
            key = {"agent_shadow": "agent", "debate": "debate"}.get(src, src)
            report["sources"][key] = {
                "label": SOURCE_LABEL.get(key, src),
                "n": r.get("count", 0), "win_rate": r.get("win_rate", 0),
                "avg_return": r.get("avg_return", 0),
                "avg_alpha": None,  # 真实逐笔 alpha 需基准对齐，见 get_excess_return
            }
    else:
        n = force_sim or 200
        report["data_source"] = "SIMULATED"
        report["note"] = (f"⚠️ 模拟数据（种子固定，每源 {n} 笔）——仅验证回测管线与口径，"
                          "不代表真实收益，不可作为实盘业绩对外陈述。真实样本累积后自动切换 live。")
        sim = _make_simulated(n)
        for src, trades in sim.items():
            s = _summarize(trades)
            s["label"] = SOURCE_LABEL[src]
            report["sources"][src] = s

    # 2) 排名（按均alpha优先，退化按均收益）
    def _key(kv):
        v = kv[1]
        return (v.get("avg_alpha") if v.get("avg_alpha") is not None else v.get("avg_return", 0)) or 0
    ranking = sorted(report["sources"].items(), key=_key, reverse=True)
    report["ranking"] = [k for k, _ in ranking]

    # 3) 结论行（辩论 vs 单Agent 的差额，最能回答"多智能体值不值"）
    src = report["sources"]
    if "debate" in src and "agent" in src:
        d, a = src["debate"], src["agent"]
        base_d = d.get("avg_alpha") if d.get("avg_alpha") is not None else d.get("avg_return")
        base_a = a.get("avg_alpha") if a.get("avg_alpha") is not None else a.get("avg_return")
        report["debate_vs_agent"] = {
            "win_rate_gap_pp": round((d.get("win_rate", 0) - a.get("win_rate", 0)), 1),
            "return_edge_pp": round(((base_d or 0) - (base_a or 0)), 2),
        }
    return report


def format_report(rep: dict) -> str:
    lines = ["═══ 三源选股对比回测 ═══",
             f"数据源: {rep.get('data_source')}", rep.get("note", ""), ""]
    header = f"{'来源':<12}{'样本':>6}{'胜率%':>8}{'均收益pp':>10}{'均alpha':>9}"
    lines.append(header)
    for src in rep.get("ranking", []):
        s = rep["sources"][src]
        alpha = s.get("avg_alpha")
        lines.append(f"{s.get('label',src):<12}{s.get('n',0):>6}{s.get('win_rate',0):>8}"
                     f"{s.get('avg_return',0):>10}{(alpha if alpha is not None else '-'):>9}")
    if rep.get("debate_vs_agent"):
        g = rep["debate_vs_agent"]
        lines += ["", f"多Agent辩论 vs 单Agent: 胜率+{g['win_rate_gap_pp']}pp, 收益+{g['return_edge_pp']}pp"]
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", type=int, default=None, help="强制模拟，指定每源笔数")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    store = None
    if args.sim is None:
        try:
            from decision_store import get_decision_store
            store = get_decision_store()
        except Exception as e:
            logger.warning("无法加载 decision_store，改用模拟: %s", e)

    rep = run_backtest(store=store, force_sim=args.sim)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else format_report(rep))
