#!/usr/bin/env python3
"""
辩论分析师专属工具箱（Multi-Agent L3 地基）

把辩论从「主程序预抽好数据、喂给分析师一次问」升级为「每个分析师拿到一小段
任务 + 一组专属只读工具，自己决定去取哪些证据」——这是从「多人格集成审议」
走向「真·Multi-Agent（每个 agent 有自主性、有自己的工具、跑自己的循环）」的关键。

设计：
  - ROLE_TOOL_SCHEMAS：{role: [function-calling schema...]}，每个分析师只看得到
    自己领域的工具（观象看技术/资金工具、观史看飞轮胜率工具、观危看集中度工具…），
    职责隔离即 prompt 隔离 + 工具隔离双保险。
  - AnalystToolCtx：黑板/共享上下文，持有本轮辩论的原料（候选池、信号、广度、
    热板块）与运行时依赖（collector、decision_store），工具从这里读，不自己造数据。
  - execute_analyst_tool(role, name, args, ctx)：路由到真实数据函数，失败返回可读文本。

与 agent_tools.py（面向终端用户 Agent）的区别：这里是「辩论内部」分析师用的、
更细粒度的取证工具，且按角色收窄可见范围。两者都复用同一批底层数据能力。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 黑板 / 共享上下文
# ═══════════════════════════════════════════════════════════════

class AnalystToolCtx:
    """一次辩论的共享原料 + 运行时依赖。分析师工具从这里取证，不自造数据。

    black_board：主席与分析师之间的共享便签（L4 用）——主席可写入定向追问、
    分析师可读到"主席让我补什么"。这是黑板式协作的载体。
    """

    def __init__(self, candidates: list[dict] = None, signals: dict = None,
                 breadth: float = 0.5, hot_sectors: list[str] = None,
                 collector=None, store=None, loop_context: str = ""):
        self.candidates = candidates or []
        self.signals = signals or {}
        self.breadth = breadth
        self.hot_sectors = hot_sectors or []
        self.collector = collector          # 提供板块资金流时序
        self.store = store                  # decision_store：飞轮历史胜率
        self.loop_context = loop_context    # 飞轮历史文本（观史兜底）
        self.black_board: dict = {}         # 主席↔分析师共享便签（L4）

    # 候选池按代码/名称索引，供个股类工具查
    def find_candidate(self, key: str) -> Optional[dict]:
        key = str(key).strip()
        for c in self.candidates:
            if str(c.get("code", "")) == key or c.get("name", "") == key:
                return c
        return None


# ═══════════════════════════════════════════════════════════════
# 工具 schema（按角色收窄可见范围）
# ═══════════════════════════════════════════════════════════════

def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                       "required": required or []}}}

# 所有角色都能看候选池概览（辩论的公共事实底座）
_TOOL_LIST_CANDIDATES = _fn(
    "list_candidates",
    "列出当前候选池全部标的的概览（代码/名称/板块/池/盘中评分/信号/涨跌幅），"
    "用于先看清全局再决定深挖哪些。所有分析师都可先调用它。")

ROLE_TOOL_SCHEMAS: dict[str, list] = {
    # 观象·技术+资金面
    "tech": [
        _TOOL_LIST_CANDIDATES,
        _fn("get_stock_technicals",
            "获取某只候选股的技术+资金面细节（盘中/日线评分、量比、换手率、涨跌幅、信号）。",
            {"code": {"type": "string", "description": "6位代码或股票名称"}}, ["code"]),
        _fn("get_sector_flow",
            "获取某板块近30分钟资金流时序与趋势（加速流入/温和/流出），判断板块动能。",
            {"sector": {"type": "string", "description": "板块名，如 白酒、半导体"}}, ["sector"]),
    ],
    # 观势·情绪面
    "sentiment": [
        _TOOL_LIST_CANDIDATES,
        _fn("get_market_sentiment",
            "获取市场情绪数据：市场广度、情绪指数、涨停/跌停家数、技术态势，判断贪婪/恐惧。"),
    ],
    # 观闻·消息面
    "news": [
        _TOOL_LIST_CANDIDATES,
        _fn("get_news_signals",
            "获取消息面信号：当日催化剂/政策/行业事件及其对板块的影响（来自消息系统）。"),
    ],
    # 观史·历史复盘（直接查飞轮，不再被动接 loop_context）
    "history": [
        _TOOL_LIST_CANDIDATES,
        _fn("list_signal_win_rates",
            "列出决策飞轮里所有信号组合的历史真实胜率与平均收益（已结算样本），"
            "用于判断哪些信号历史上真赚钱。"),
        _fn("get_signal_win_rate",
            "查询某个具体信号组合的历史胜率（精确校验候选票所属信号的历史表现）。",
            {"signal": {"type": "string", "description": "信号名或 池-信号 组合，如 放量上攻"}}, ["signal"]),
        _fn("get_source_performance",
            "对比不同决策来源(单Agent/多Agent辩论/规则)的历史胜率，看哪条路径更准。"),
    ],
    # 观危·风控
    "risk": [
        _TOOL_LIST_CANDIDATES,
        _fn("get_sector_concentration",
            "计算候选池的板块集中度分布（各板块占比），识别是否过度集中于单一主题。"),
        _fn("get_market_regime",
            "获取大盘环境判定：市场广度、普跌/分化/偏多，用于判断是否支持进攻仓位。"),
    ],
}


# ═══════════════════════════════════════════════════════════════
# 执行器
# ═══════════════════════════════════════════════════════════════

def tools_for(role: str) -> list:
    """返回某角色可用的工具 schema 列表。"""
    return ROLE_TOOL_SCHEMAS.get(role, [_TOOL_LIST_CANDIDATES])


def execute_analyst_tool(role: str, name: str, args: dict, ctx: AnalystToolCtx) -> str:
    """执行分析师工具调用，返回给 LLM 的文本结果（失败也返回可读文本，不抛异常）。"""
    try:
        if name == "list_candidates":
            return _t_list_candidates(ctx)
        if name == "get_stock_technicals":
            return _t_stock_technicals(args.get("code", ""), ctx)
        if name == "get_sector_flow":
            return _t_sector_flow(args.get("sector", ""), ctx)
        if name == "get_market_sentiment":
            return _t_market_sentiment(ctx)
        if name == "get_news_signals":
            return _t_news_signals(ctx)
        if name == "list_signal_win_rates":
            return _t_list_win_rates(ctx)
        if name == "get_signal_win_rate":
            return _t_signal_win_rate(args.get("signal", ""), ctx)
        if name == "get_source_performance":
            return _t_source_performance(ctx)
        if name == "get_sector_concentration":
            return _t_sector_concentration(ctx)
        if name == "get_market_regime":
            return _t_market_regime(ctx)
        return f"未知工具: {name}"
    except Exception as e:
        logger.warning("分析师工具 %s 执行失败: %s", name, e)
        return f"工具 {name} 执行失败: {e}（可基于已有信息判断）"


# ── 公共 ──────────────────────────────────────────────────────
def _t_list_candidates(ctx: AnalystToolCtx) -> str:
    ranked = sorted(ctx.candidates, key=lambda x: x.get("score", 0), reverse=True)[:20]
    rows = [{
        "代码": s.get("code", ""), "名称": s.get("name", ""),
        "板块": s.get("sector", ""), "池": s.get("pool", ""),
        "评分": s.get("score", 0), "信号": s.get("signal", "?"),
        "涨跌幅": f"{s.get('pct_chg', 0):+.1f}%",
    } for s in ranked]
    return json.dumps({"候选池规模": len(ctx.candidates), "Top20": rows}, ensure_ascii=False)


# ── 观象 ──────────────────────────────────────────────────────
def _t_stock_technicals(code: str, ctx: AnalystToolCtx) -> str:
    c = ctx.find_candidate(code)
    if not c:
        return f"候选池中未找到 {code}（只能分析候选池内标的）"
    ds = c.get("daily_combined_score")
    return json.dumps({
        "代码": c.get("code"), "名称": c.get("name"), "板块": c.get("sector"), "池": c.get("pool"),
        "盘中评分": c.get("score", 0), "日线评分": (f"{ds:.1f}" if ds is not None else "?"),
        "涨跌幅": f"{c.get('pct_chg', 0):+.1f}%", "信号": c.get("signal", "?"),
        "量比": c.get("volume_ratio", 0), "换手率": c.get("turnover_rate", 0),
    }, ensure_ascii=False)


def _t_sector_flow(sector: str, ctx: AnalystToolCtx) -> str:
    if not sector:
        return "请提供板块名称"
    if not ctx.collector:
        return "板块资金流数据未接入"
    ts = ctx.collector.get_sector_timeseries(sector, recent_minutes=30)
    if not ts or ts.get("error"):
        return f"未找到板块「{sector}」的时序数据（可能不在监控白名单）"
    return json.dumps({
        "板块": ts.get("name"), "最新净流入(亿)": ts.get("latest_value"),
        "最新净占比": ts.get("latest_ratio"), "趋势": ts.get("trend"),
        "数据点数": ts.get("data_points"),
    }, ensure_ascii=False)


# ── 观势 ──────────────────────────────────────────────────────
def _t_market_sentiment(ctx: AnalystToolCtx) -> str:
    sig = ctx.signals or {}
    out = {"市场广度": f"{int(ctx.breadth * 100)}% 板块上涨"}
    # 从信号字典里尽量抽取情绪相关字段（兼容不同结构）
    for k in ("sentiment", "mood_score", "limit_up", "limit_down", "tech_state", "breadth"):
        if k in sig:
            out[k] = sig[k]
    stock_sig = sig.get("stock") if isinstance(sig.get("stock"), dict) else {}
    market_sig = sig.get("market") if isinstance(sig.get("market"), dict) else {}
    if market_sig:
        out["大盘信号"] = market_sig
    if stock_sig:
        out["个股情绪摘要"] = {k: stock_sig[k] for k in list(stock_sig)[:6]}
    return json.dumps(out, ensure_ascii=False, default=str)[:1500]


# ── 观闻 ──────────────────────────────────────────────────────
def _t_news_signals(ctx: AnalystToolCtx) -> str:
    sig = ctx.signals or {}
    news = sig.get("news") if isinstance(sig.get("news"), dict) else {}
    if not news:
        # 兜底：从信号里找任何消息相关键
        news = {k: v for k, v in sig.items() if any(t in str(k) for t in ("news", "catalyst", "event", "消息"))}
    if not news:
        return json.dumps({"提示": "当前无结构化消息面信号，请基于候选池板块与常识审慎判断，不要编造具体新闻"}, ensure_ascii=False)
    return json.dumps({"消息面信号": news}, ensure_ascii=False, default=str)[:1500]


# ── 观史 ──────────────────────────────────────────────────────
def _t_list_win_rates(ctx: AnalystToolCtx) -> str:
    if not ctx.store:
        return ctx.loop_context or "飞轮历史数据未接入"
    try:
        rows = ctx.store.get_signal_win_rates()
    except Exception as e:
        return f"查询飞轮胜率失败: {e}"
    if not rows:
        return json.dumps({"提示": "飞轮暂无足够已结算样本（信号胜率尚在积累）"}, ensure_ascii=False)
    return json.dumps({"信号历史胜率": [
        {"信号": r.get("signal_combo"), "样本数": r.get("total_trades"),
         "胜率": f"{r.get('win_rate')}%", "均收益": f"{r.get('avg_ret'):+.2f}%"}
        for r in rows[:15]
    ]}, ensure_ascii=False)


def _t_signal_win_rate(signal: str, ctx: AnalystToolCtx) -> str:
    if not signal:
        return "请提供信号名"
    if not ctx.store:
        return ctx.loop_context or "飞轮历史数据未接入"
    try:
        rows = ctx.store.get_signal_win_rates()
    except Exception as e:
        return f"查询失败: {e}"
    hits = [r for r in rows if signal in str(r.get("signal_combo", ""))]
    if not hits:
        return json.dumps({"信号": signal, "结论": "历史样本不足或无记录，需谨慎（不可假设其历史有效）"}, ensure_ascii=False)
    return json.dumps({"匹配信号": [
        {"信号": r.get("signal_combo"), "样本数": r.get("total_trades"),
         "胜率": f"{r.get('win_rate')}%", "均收益": f"{r.get('avg_ret'):+.2f}%"} for r in hits
    ]}, ensure_ascii=False)


def _t_source_performance(ctx: AnalystToolCtx) -> str:
    if not ctx.store:
        return "飞轮数据未接入"
    try:
        rows = ctx.store.get_source_win_rates()
    except Exception as e:
        return f"查询失败: {e}"
    if not rows:
        return json.dumps({"提示": "各来源均无已结算样本，无法对比"}, ensure_ascii=False)
    label = {"agent_shadow": "单Agent选股", "debate": "多Agent辩论", "agent": "Agent其他"}
    return json.dumps({"来源胜率对比": [
        {"来源": label.get(r["source"], r["source"]), "样本": r["count"],
         "胜率": f"{r['win_rate']}%", "均收益": f"{r['avg_return']:+.2f}%"} for r in rows
    ]}, ensure_ascii=False)


# ── 观危 ──────────────────────────────────────────────────────
def _t_sector_concentration(ctx: AnalystToolCtx) -> str:
    from collections import Counter
    sample = ctx.candidates[:25]
    sectors = Counter(c.get("sector", "未知") for c in sample)
    total = sum(sectors.values()) or 1
    top = sectors.most_common(6)
    top_share = (top[0][1] / total * 100) if top else 0
    return json.dumps({
        "样本数": len(sample),
        "板块分布": [{"板块": s, "占比": f"{n/total*100:.0f}%"} for s, n in top],
        "最大单板块占比": f"{top_share:.0f}%",
        "集中度判定": ("过度集中(系统性风险)" if top_share >= 50 else
                       ("偏集中(需警惕)" if top_share >= 35 else "分散(较健康)")),
    }, ensure_ascii=False)


def _t_market_regime(ctx: AnalystToolCtx) -> str:
    b = ctx.breadth
    regime = "普跌/防守" if b < 0.3 else ("分化/中性" if b < 0.5 else "偏多/可进攻")
    return json.dumps({
        "市场广度": f"{int(b * 100)}% 板块上涨",
        "环境判定": regime,
        "热板块": ctx.hot_sectors[:8],
        "仓位倾向": ("轻仓/观望" if b < 0.3 else ("半仓" if b < 0.5 else "可积极")),
    }, ensure_ascii=False)
