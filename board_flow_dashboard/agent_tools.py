#!/usr/bin/env python3
"""
观澜 Agent 工具层（Function Calling）

把系统已有的数据能力封装成 LLM 可自主调用的「工具」，是观澜从
「上下文增强问答」升级为「具备工具调用能力的 Agent」的关键一环。

设计：
  - TOOL_SCHEMAS：OpenAI function-calling 格式的工具定义（告诉 LLM 有哪些工具、怎么调）
  - execute_tool(name, args, ctx)：工具执行器，把 LLM 的调用意图路由到真实数据函数
  - 工具全部复用现有能力（诊断/板块时序/候选池/大盘信号），不重复造轮子
  - ctx 注入运行时依赖（collector 等），避免循环 import

工具清单：
  1. get_stock_diagnosis(code)  — 个股诊断（实时行情+信号+K线上下文）
  2. get_sector_trend(name)     — 板块资金流时序趋势（判断板块启动/退潮）
  3. get_candidate_pool()       — 当前 A/B 候选池（选股推荐）
  4. get_market_signals()       — 大盘/情绪/三系统信号
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── 工具结果短 TTL 缓存 ────────────────────────────────────────
# 盘中数据分钟级刷新即可，同一 (工具,参数) 在 TTL 内重复调用直接命中，
# 避免一次对话里多次问同一只票 / 候选池被反复重算（get_candidate_pool 尤重）。
_CACHE_TTL = 60.0  # 秒
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> Optional[str]:
    with _cache_lock:
        item = _cache.get(key)
        if item and (time.time() - item[0]) < _CACHE_TTL:
            return item[1]
        if item:
            _cache.pop(key, None)  # 过期清理
    return None


def _cache_set(key: str, value: str):
    with _cache_lock:
        _cache[key] = (time.time(), value)


# ── 工具定义（OpenAI function-calling schema）──────────────────
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_diagnosis",
            "description": "获取某只 A 股的实时诊断数据，包括实时价、涨跌幅、技术/情绪信号、K线关键事件、量价指标。当用户询问某只具体股票（给出代码或名称）时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位 A 股代码，如 600519"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_trend",
            "description": "获取某个板块近 30 分钟的资金流时序与趋势（加速流入/温和流入/资金平稳/温和流出/加速流出），用于判断板块是刚启动还是已退潮。当用户询问板块情况或需要判断板块动能时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "板块名称，如 白酒、半导体、人形机器人"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_candidate_pool",
            "description": "获取当前盘中选股候选池（A 池追涨 + B 池低吸），含每只票的评分、信号、板块。当用户问「现在买什么/有什么机会/推荐个股」时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_signals",
            "description": "获取大盘环境信号：市场广度、情绪指数、三系统（技术/情绪/消息）状态。当用户问大盘、市场情绪、仓位建议时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ── 写类工具定义（Agent「长出手」：能执行动作，非只读）──────────
# 这些工具是「提议式」的：Agent 调用它们不会立即改数据，而是登记一个
# 「待确认动作」，由前端向用户展示确认卡片、用户点确认后才真正执行。
# 这是人机协作的安全设计——Agent 能提议写操作，但执行权始终握在用户手里。
WRITE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "propose_record_decision",
            "description": "当用户明确表达『看好某只票、打算买入/建仓』时，提议把这笔决策记入决策飞轮（用于后续复盘与胜率统计）。这是提议，不会立即执行，需用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位股票代码"},
                    "name": {"type": "string", "description": "股票名称"},
                    "entry_price": {"type": "number", "description": "计划买入价"},
                    "signal": {"type": "string", "description": "对应信号，如 放量上攻（可选）"},
                    "reason": {"type": "string", "description": "一句话记录看好理由"},
                },
                "required": ["code", "entry_price"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_add_watch",
            "description": "当用户表达『帮我盯着某只票、加入盯盘、买了想被提醒』时，提议把该票加入持仓盯盘助手。这是提议，不会立即执行，需用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位股票代码"},
                    "name": {"type": "string", "description": "股票名称"},
                    "cost": {"type": "number", "description": "持仓成本价"},
                    "signal": {"type": "string", "description": "对应信号（可选，用于自动定止盈止损位）"},
                },
                "required": ["code", "cost"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_set_alert",
            "description": "当用户想为某只持仓设置/调整止盈止损提醒点位时，提议设置提醒。这是提议，不会立即执行，需用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位股票代码"},
                    "take_profit_pct": {"type": "number", "description": "止盈百分比，如 6 表示 +6%（可选）"},
                    "stop_loss_pct": {"type": "number", "description": "止损百分比，如 -4 表示 -4%（可选）"},
                },
                "required": ["code"],
            },
        },
    },
]

# 写工具名 → 给用户看的动作类型（前端据此渲染确认卡片）
WRITE_TOOL_ACTIONS = {
    "propose_record_decision": "record_decision",
    "propose_add_watch": "add_watch",
    "propose_set_alert": "set_alert",
}



class ToolContext:
    """工具执行所需的运行时依赖（避免循环 import，由调用方注入）。"""

    def __init__(self, collector=None, extract_stock_context: Optional[Callable] = None,
                 select_stocks: Optional[Callable] = None, get_signal_store: Optional[Callable] = None,
                 enable_write: bool = False):
        self.collector = collector
        self.extract_stock_context = extract_stock_context   # app._extract_stock_context
        self.select_stocks = select_stocks                   # stock_selector.select_stocks
        self.get_signal_store = get_signal_store             # signals.get_signal_store
        self.enable_write = enable_write                     # 是否放开写类工具
        self.pending_actions = []                            # 收集 Agent 提议的待确认动作


def _tool_propose(name: str, args: dict, ctx: ToolContext) -> str:
    """写类工具的提议式执行：登记待确认动作，返回给 LLM 的确认提示（不落库）。"""
    action_type = WRITE_TOOL_ACTIONS.get(name)
    if not action_type:
        return f"未知写工具: {name}"
    import re
    code = str(args.get("code", "")).strip()
    if not re.match(r"^\d{6}$", code):
        return f"股票代码 {code} 格式错误（需 6 位数字），未生成动作"
    ctx.pending_actions.append({"type": action_type, "params": dict(args)})
    labels = {"record_decision": "记入决策飞轮", "add_watch": "加入持仓盯盘", "set_alert": "设置止盈止损提醒"}
    return (f"已为 {code} 生成「{labels.get(action_type, action_type)}」的待确认动作。"
            f"请在回复中简要说明该动作，并提示用户需点击确认后才会执行。")


# ── 工具执行器 ────────────────────────────────────────────────
def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """执行工具调用，返回给 LLM 的文本结果（失败也返回可读文本，不抛异常）。

    带 60s TTL 缓存：同一 (工具,参数) 短时间内重复调用直接命中缓存。
    """
    # 写类工具：提议式，不缓存、不直接落库
    if name in WRITE_TOOL_ACTIONS:
        return _tool_propose(name, args, ctx)
    cache_key = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("工具缓存命中: %s", cache_key)
        return cached
    try:
        if name == "get_stock_diagnosis":
            result = _tool_stock_diagnosis(args.get("code", ""), ctx)
        elif name == "get_sector_trend":
            result = _tool_sector_trend(args.get("name", ""), ctx)
        elif name == "get_candidate_pool":
            result = _tool_candidate_pool(ctx)
        elif name == "get_market_signals":
            result = _tool_market_signals(ctx)
        else:
            return f"未知工具: {name}"
    except Exception as e:
        logger.warning("工具 %s 执行失败: %s", name, e)
        return f"工具 {name} 执行失败: {e}（可基于其他信息回答，或提示数据暂不可用）"
    # 失败/空数据文本不缓存，避免掩盖数据源恢复
    if result and not any(k in result[:20] for k in ("未能获取", "未找到", "执行失败", "未接入", "格式错误")):
        _cache_set(cache_key, result)
    return result


def _tool_stock_diagnosis(code: str, ctx: ToolContext) -> str:
    import re
    code = str(code).strip()
    if not re.match(r"^\d{6}$", code):
        return f"股票代码 {code} 格式错误（需 6 位数字）"
    if not ctx.extract_stock_context:
        return "个股诊断能力未接入"
    stock_ctx, _kline = ctx.extract_stock_context(code)
    if not stock_ctx:
        return f"未能获取 {code} 的实时数据（可能非交易时段或代码无效）"
    return stock_ctx


def _tool_sector_trend(name: str, ctx: ToolContext) -> str:
    name = str(name).strip()
    if not name:
        return "请提供板块名称"
    if not ctx.collector:
        return "板块数据能力未接入"
    ts = ctx.collector.get_sector_timeseries(name, recent_minutes=30)
    if not ts or ts.get("error"):
        return f"未找到板块「{name}」的时序数据（板块名可能不在监控白名单）"
    return json.dumps({
        "板块": ts.get("name"),
        "最新净流入(亿)": ts.get("latest_value"),
        "最新净占比": ts.get("latest_ratio"),
        "趋势": ts.get("trend"),
        "数据点数": ts.get("data_points"),
    }, ensure_ascii=False)


def _tool_candidate_pool(ctx: ToolContext) -> str:
    if not ctx.select_stocks:
        return "选股能力未接入"
    data = ctx.select_stocks(collector=ctx.collector) if ctx.collector else ctx.select_stocks()
    pool_a = data.get("pool_a", [])[:10]
    pool_b = data.get("pool_b", [])[:10]

    def _fmt(s):
        return {
            "代码": s.get("code"), "名称": s.get("name"),
            "板块": s.get("sector"), "评分": s.get("score"),
            "信号": s.get("signal"), "涨跌幅": s.get("pct_chg"),
        }

    return json.dumps({
        "A池_追涨(Top10)": [_fmt(s) for s in pool_a],
        "B池_低吸(Top10)": [_fmt(s) for s in pool_b],
        "热板块": data.get("hot_sectors", [])[:8],
        "市场广度": data.get("market_breadth"),
    }, ensure_ascii=False)


def _tool_market_signals(ctx: ToolContext) -> str:
    if not ctx.get_signal_store:
        return "信号系统未接入"
    signals = ctx.get_signal_store().get_all()
    return json.dumps(signals, ensure_ascii=False, default=str)[:2000]
