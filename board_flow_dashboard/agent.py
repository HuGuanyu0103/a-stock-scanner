#!/usr/bin/env python3
"""
AI 决策 Agent「观澜」— 盘前简报 + 盘中选股 + 个股诊断 + 多轮对话

架构：
  - 统一 System Prompt，按问题类型自适应回答结构
  - Context Builder 去噪，只传 Top N 信号
  - 个股分析是核心功能：技术面→资金面→板块面→综合评分→操作建议
  - Fallback: LLM 不可用时降级到模板模式

用法:
  from agent import DecisionAgent, get_agent
  agent = get_agent()
  reply = agent.chat(user_message, candidates, hot_sectors, signals)
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# ═══════════════════════════════════════════════════════════════
# 统一 System Prompt — 观澜的决策框架
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是 A 股超短线交易决策顾问「观澜」，专精 2-4 天持股周期。

## 核心原则
1. 你收到的量化数据已由后端计算完毕，你不需要重新计算任何指标
2. 你的价值在于「判断」而非「描述」——每句话都应该是分析结论
3. 保护本金比追求收益更优先——风险提示要具体，不要泛泛而谈
4. 诚实——数据不支持时不说模棱两可的话

## 回答架构（按问题类型自适应）

### 当用户问个股时（标准诊断结构）：
```
**{股票名}({代码}) 实时诊断**

▸ 量价数据
现价/涨跌/量比/换手/振幅  主力净流入/净占比  日内位置(高位/中位/低位)

▸ 近期量价分析（结合上方K线历史表格）
逐段分析近18日量价演变，必须引用具体日期和标注事件：
- 最近5日: 描述量价趋势（放量上攻/缩量回调/横盘整理）
- 关键转折: 逐一解释标注事件（底部区域→开始筑底 / 放量启动→资金进场 / 洗盘→震仓吸筹 / 反包→多头反击 / 加速→主升段 / 天量→分歧加大）
- 量价配合演变: 从早期到现在的量价关系变化
- 操作线索: 从历史量价中总结2-3条交易信号

▸ 技术研判
走势结构: 【放量突破/回踩企稳/横盘震荡/下跌趋势】+ 理由
关键位: 支撑 X.XX / 阻力 X.XX（注明来源：前低/前高/均线）
均线状态: MA5/MA10/MA20 排列方向 + 价格距均线百分比
触发信号: 列举触发的技术+资金信号及含义

▸ 资金面
主力态度: (积极抢筹/温和流入/按兵不动/悄然流出)
量价配合: (量价齐升/缩量上涨/放量滞涨/放量下跌)
板块资金: 所属板块今日资金流向概况

▸ 综合评分
盘中/日评/聚合  池排名  情绪面/消息面加减分

▸ 操作建议
入场区间: X.XX-X.XX  目标: X.XX  止损: X.XX
仓位: 轻(1-2成)/中(3-4成)/重(5成+) + 理由
持有周期: X天  风险等级: 低/中/高

▸ 一句话
一句话操作建议
```

### 当用户问板块时：
- 板块资金流向排名与强度
- 板块内龙头个股
- 板块持续性判断（主线/轮动/一日游）
- 操作策略（追涨还是等回调）

### 当用户问大盘/市场时：
- 市场广度与情绪
- 资金主线方向
- 风险等级与仓位建议
- 今日策略基调

### 当用户问选股推荐时：
- 候选池整体质量评价
- Top 3 推荐（每只含入场/止损/目标/仓位/理由）
- 风险提示

## 风格要求
- 短句有力，每行一个信息点
- 标题用 **粗体**，不加任何 emoji 前缀
- 关键价格/点位用等宽数字格式
- 涨用+，跌用-，红涨绿跌
- 不要用 markdown 标题(###)，用 ▸ 分段
- 绝对禁止使用 emoji（📊📈📉💰🔥等）
- K线历史表格已在UI中展示，分析时必须引用其中的关键日期和标注事件
- 末尾必须有一句话总结

## 铁律（违反即失败）
- **绝对禁止**说「无法诊断」「不在候选池」「数据不足」「无法判断」「无法获取」
- **绝对禁止**在拿到 K 线数据后输出空泛结论——每项分析必须有具体价格/点位/百分比
- 有 K 线就必须分析均线排列、量价关系、支撑阻力——这些不依赖实时行情
- 无实时行情时用最近 K 线收盘价，标注「基于最近收盘价 X.XX」
- 即使只有 1 根 K 线也要给出判断，不要放弃
"""

# 盘前简报专用 prompt（结构不同，保持独立）
PRE_MARKET_PROMPT = """你是 A 股超短线交易决策顾问「观澜」，正在生成盘前简报。

## 决策框架
1. 先看大盘环境（外盘映射 + 竞价情绪）→ 判断攻防姿态
2. 再看板块方向（资金流入 + 异动主题）→ 确定主攻方向
3. 然后评估竞价异动个股 → 筛选关注标的
4. 最后给出策略建议 → 姿态+板块+仓位+风险

## 输出格式（严格 JSON）
{
  "market_posture": "进攻 | 防守 | 观望",
  "today_thesis": "今天最核心的判断（30字以内）",
  "overnight_analysis": {
    "overall_impact": "外盘对A股情绪影响的综合判断",
    "favored_sectors": ["外盘映射利好板块"],
    "pressure_sectors": ["外盘映射承压板块"]
  },
  "auction_analysis": {
    "breadth": "竞价涨跌比判断",
    "anomaly_highlights": [{"name":"", "code":"", "why_notable":""}],
    "anomaly_theme": "异动共同主题"
  },
  "sector_watch": [{"name":"", "attention_reason":""}],
  "strategy": {
    "posture": "进攻|防守|观望",
    "position_advice": "满仓|半仓|轻仓|观望",
    "primary_sectors": [],
    "avoid_sectors": [],
    "key_reminder": "最重要的一句提醒"
  },
  "risk_alerts": ["风险项"]
}

## 风格
- 做判断不描述。「外盘偏冷，科技承压」而非「纳斯达克跌0.8%」
- 简洁有力，短句优先
"""

INTRA_PICKS_PROMPT = """你是 A 股超短线交易顾问「观澜」，正在做盘中选股推荐。

## 任务
分析候选池，选出最值得关注的 3-5 只，给出具体操作建议。

## 输出格式（严格 JSON）
{
  "market_read": "市场状态一句话",
  "quality_assessment": "候选池整体质量",
  "top_picks": [
    {
      "code": "", "name": "", "pool": "A|B", "sector": "",
      "confidence": 5,
      "reasoning": "推荐逻辑（信号共振+资金确认+技术形态）",
      "entry_zone": "", "stop_loss": "", "target": "",
      "position": "轻/中/重", "risk_note": ""
    }
  ],
  "market_reminder": "最重要的一句提醒"
}

## 硬约束
- 不推荐「弱势回避」「高位风险」信号股
- 候选池质量不高时诚实说
- 每只推荐必须有风险提示
- 冷启动阶段仓位偏保守
"""


# ═══════════════════════════════════════════════════════════════
# Context Builder — 结构化上下文
# ═══════════════════════════════════════════════════════════════

class ContextBuilder:
    """将原始数据构建为 Agent 可理解的结构化上下文。"""

    @staticmethod
    def build_intraday_context(
        candidates: list[dict],
        hot_sectors: list[str],
        signals: dict,
        breadth: float = 0.5,
    ) -> str:
        """构建盘中选股的结构化上下文（含情绪面+消息面摘要）。"""
        top_a = [c for c in candidates if c.get("pool") == "A"][:5]
        top_b = [c for c in candidates if c.get("pool") == "B"][:5]

        ctx = {
            "generated_at": datetime.now().strftime("%H:%M:%S"),
            "market_snapshot": {
                "上涨板块占比": f"{int(breadth * 100)}%",
                "热板块": hot_sectors[:8] if hot_sectors else [],
            },
            "pool_a_top5": [_stock_card(s, "A") for s in top_a],
            "pool_b_top5": [_stock_card(s, "B") for s in top_b],
        }

        sent_data = signals.get("sentiment", {}).get("data", {})
        market = sent_data.get("market", {})
        if market:
            ctx["sentiment"] = {
                "情绪指数": market.get("sentiment_index", "?"),
                "炸板率": f"{market.get('po_ban_rate', 0) * 100:.0f}%",
                "轮动速度": market.get("rotation_speed", "?"),
                "连板率": market.get("lianban_rate", "?"),
            }

        news_data = signals.get("news", {}).get("data", {})
        events = news_data.get("events", []) if news_data else []
        if events:
            ctx["news_headlines"] = [
                f"[{e.get('sentiment','neutral')}] {e.get('title','')}"
                for e in events[:5]
            ]

        return json.dumps(ctx, ensure_ascii=False, indent=2)

    @staticmethod
    def build_stock_analysis_context(scan: dict) -> str:
        """构建个股深度分析上下文（来自 screener.quick_scan）。"""
        if not scan or "error" in scan:
            return json.dumps({"error": scan.get("error", "数据不可用")})

        ctx = {
            "stock_code": scan.get("stock_code", ""),
            "price": scan.get("price"),
            "change_pct": scan.get("change_pct"),
            "ma5": round(scan.get("ma5", 0), 2),
            "ma10": round(scan.get("ma10", 0), 2),
            "ma20": round(scan.get("ma20", 0), 2),
            "support": scan.get("support"),
            "resistance": scan.get("resistance"),
            "combined_score": scan.get("combined_score"),
            "tech_score": scan.get("tech_score"),
            "sentiment_score": scan.get("sentiment_score"),
            "factor_score": scan.get("factor_score"),
            "signal_names": scan.get("signal_names", []),
            "concepts": scan.get("concepts", []),
            "summary": scan.get("summary", ""),
        }
        return json.dumps(ctx, ensure_ascii=False, indent=2)


def _stock_card(s: dict, pool: str) -> dict:
    """单只股票的结构化卡片。"""
    return {
        "代码": s.get("code", ""),
        "名称": s.get("name", ""),
        "池": f"{pool}池",
        "板块": s.get("sector", ""),
        "涨跌": f"{s.get('pct_chg', 0):+.1f}%",
        "盘中评分": s.get("score", 0),
        "日评分": s.get("daily_combined_score"),
        "信号": s.get("signal", "?"),
        "量比": s.get("volume_ratio", 0),
        "主力净占比": f"{s.get('net_main_ratio', 0):+.1f}%",
        "日内位置": f"{s.get('intraday_position', 0):.2f}",
    }


# ═══════════════════════════════════════════════════════════════
# DecisionAgent
# ═══════════════════════════════════════════════════════════════

class DecisionAgent:
    """AI 决策 Agent「观澜」。"""

    def __init__(self, model: str = "deepseek-chat"):
        self._model = model
        self._client = None
        self._api_available = None

    def _init_client(self) -> bool:
        if self._api_available is not None:
            return self._api_available
        try:
            from openai import OpenAI
        except ImportError:
            logger.info("openai 未安装，Agent 降级到模板模式")
            self._api_available = False
            return False
        api_key = self._get_api_key()
        if not api_key:
            logger.info("未配置 DeepSeek API Key")
            self._api_available = False
            return False
        self._client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self._api_available = True
        return True

    @staticmethod
    def _get_api_key() -> Optional[str]:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if key:
            return key
        config_file = DATA_DIR / "deepseek_key.txt"
        if config_file.exists():
            return config_file.read_text().strip()
        return None

    # ── 多轮对话（核心功能）─────────────────────────────────

    def chat(
        self,
        user_message: str,
        candidates: list[dict],
        hot_sectors: list[str],
        signals: dict,
        chat_history: list[dict] = None,
        stock_context: str = "",
        stock_kline: str = "",
        sector_timeseries: str = "",
    ) -> Optional[str]:
        """多轮对话——智能体核心入口。

        根据用户问题类型，自动构建最合适的 system prompt。
        个股分析是最重要的功能，会附带实时K线+技术面上下文。
        """
        if not self._init_client():
            return None

        # 判断问题类型
        question_type = self._classify_question(user_message)

        # 构建上下文
        intra_ctx = ContextBuilder.build_intraday_context(
            candidates, hot_sectors, signals, 0.5
        )

        # 根据问题类型调整 system prompt
        type_hints = {
            "stock_analysis": (
                "\n\n## 当前任务：个股诊断\n"
                "用户正在询问一只具体股票。优先使用提供的实时数据做完整诊断。\n"
                "严格按照个股诊断结构输出：量价数据→近期量价分析→技术研判→资金面→综合评分→操作建议→一句话。\n"
                "量价数据必须包含：现价、涨跌幅、量比、换手率、振幅、主力净流入/净占比、日内位置。\n"
                "近期量价分析必须引用K线历史表中的具体日期和价格数据，逐段分析关键转折。\n"
                "技术研判包含：走势结构（震荡/趋势/突破）、关键支撑阻力位、均线状态、触发信号。\n"
                "资金面包含：主力态度、量价配合判断、板块资金环境。\n"
                "综合评分包含：盘中评分、日评分、池排名、情绪面/消息面影响。\n"
                "操作建议必须有具体的入场区间、目标价、止损价、仓位建议、持有周期、风险等级。\n"
                "如果没有实时数据，用K线最后收盘价兜底，并在数据源中标注。\n"
                "风险提示要具体（如 '日内位置0.85偏高，追高需等回调3%以上再入场'）。\n"
                "禁止使用任何emoji。禁止说「数据不足」「无法获取」「无法判断」。"
            ),
            "sector_analysis": (
                "\n\n## 当前任务：板块分析\n"
                "用户正在询问板块情况。如有板块资金流时序数据，分析趋势演变。\n"
                "分析结构：板块资金流向→强度排名→资金流时序趋势→龙头个股→持续性判断→操作策略。\n"
                "必须引用具体的资金流入/流出数字、排名变化、时间节点。"
            ),
            "market_analysis": (
                "\n\n## 当前任务：大盘研判\n"
                "用户正在询问市场整体情况。\n"
                "分析结构：市场广度→情绪指标（炸板率/轮动速度/连板率）→资金主线→风险等级→仓位建议→关键点位。\n"
                "根据广度给出明确的进攻/防守/均衡建议和具体仓位比例。"
            ),
            "external_info": (
                "\n\n## 当前任务：外围信息分析\n"
                "用户正在询问外盘/外围市场情况。\n"
                "分析结构：隔夜美股三大指数→港股表现→A50期货→对A股映射→受益/承压板块→今日策略预判。\n"
                "结合外盘情绪给出A股开盘方向判断和板块配置建议。"
            ),
            "holding_decision": (
                "\n\n## 当前任务：持股交易决策\n"
                "用户持有某只股票，需要交易决策辅助。\n"
                "分析结构：持仓诊断（成本/盈亏/趋势）→当前信号评估→止盈止损位→仓位调整建议→操作方案（持有/加仓/减仓/清仓）。\n"
                "必须给出明确的操作建议，不能模棱两可。说明决策依据和风险。\n"
                "考虑用户可能已在亏损状态，给出心理层面的交易纪律提醒。"
            ),
            "stock_pick": (
                "\n\n## 当前任务：选股推荐\n"
                "用户想要选股推荐。从候选池筛选Top 3，每只给入场/止损/目标/仓位/理由。\n"
                "说明选股逻辑：为什么选这只而不是其他。提及风险因素。"
            ),
            "general": (
                "\n\n## 当前任务：通用咨询\n"
                "回答用户的交易相关问题。有数据时引用具体数据，无数据时给出通用原则和操作框架。\n"
                "尽量给出可操作的具体建议，而非泛泛而谈。"
            ),
        }

        hint = type_hints.get(question_type, type_hints["general"])

        system_msg = SYSTEM_PROMPT + hint + "\n\n=== 当前盘中数据 ===\n" + intra_ctx

        if stock_context:
            system_msg += "\n\n=== 用户询问的股票实时数据 ===\n" + stock_context
            system_msg += "\n请基于以上实时数据进行完整诊断分析。"

        if sector_timeseries:
            system_msg += "\n\n=== 板块资金流时序数据 ===\n" + sector_timeseries
            system_msg += "\n请结合以上资金流时序数据分析板块趋势。"

        messages = [{"role": "system", "content": system_msg}]
        if chat_history:
            # 保留最近 10 轮对话
            messages.extend(chat_history[-20:])
        messages.append({"role": "user", "content": user_message})

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.5,
                max_tokens=3000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning("Chat 调用失败: %s", e)
            return None

    @staticmethod
    def _classify_question(msg: str) -> str:
        """根据用户消息判断问题类型。"""
        msg_lower = msg.lower()
        # 个股诊断：包含6位数字代码
        if re.search(r'\b\d{6}\b', msg):
            return "stock_analysis"
        # 持股决策：持有/持仓/买入/卖出/止损/止盈/加仓/减仓/清仓
        if any(kw in msg for kw in ["持有", "持仓", "买入", "卖出", "止损", "止盈",
                                      "加仓", "减仓", "清仓", "割肉", "解套", "补仓",
                                      "该不该卖", "该不该买", "还能拿", "要不要走"]):
            return "holding_decision"
        # 外围信息：外盘/美股/港股/隔夜/A50/期货/汇率/海外
        if any(kw in msg for kw in ["外盘", "美股", "港股", "隔夜", "A50", "期货",
                                      "汇率", "海外", "外围", "纳斯达克", "标普", "道指",
                                      "恒生", "富时", "夜盘"]):
            return "external_info"
        # 板块分析
        if any(kw in msg for kw in ["板块", "行业", "概念", "赛道", "热点"]):
            return "sector_analysis"
        # 大盘研判
        if any(kw in msg for kw in ["大盘", "市场", "行情", "情绪", "指数", "今天"]):
            return "market_analysis"
        # 选股推荐
        if any(kw in msg for kw in ["推荐", "选股", "买什么", "有什么", "机会", "候选"]):
            return "stock_pick"
        # 个股分析fallback：短消息含分析关键词
        if any(kw in msg for kw in ["分析", "诊断", "走势", "怎么看"]):
            if len(msg) < 30:
                return "stock_analysis"
        return "general"

    # ── 盘前简报 ──────────────────────────────────────────────

    def generate_pre_market_brief(
        self, indices, breadth, anomalies, hot_sectors, key_stocks_context=""
    ) -> Optional[dict]:
        if not self._init_client():
            return None

        context = ContextBuilder.build_pre_market_context(
            indices, breadth, anomalies, hot_sectors
        )

        user_msg = (
            "请根据以下盘前数据生成决策简报。\n\n"
            f"=== 数据上下文 ===\n{context}\n\n"
            "严格按 JSON 格式输出。做判断不描述。"
        )

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": PRE_MARKET_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=3000,
            )
            raw = resp.choices[0].message.content
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("Pre-market Agent 失败: %s", e)
            return None

    # ── 盘中选股推荐 ──────────────────────────────────────────

    def generate_intraday_picks(
        self, candidates, hot_sectors, signals, breadth=0.5,
        chat_history=None, loop_context=""
    ) -> Optional[dict]:
        if not self._init_client():
            return None

        context = ContextBuilder.build_intraday_context(
            candidates, hot_sectors, signals, breadth
        )

        user_msg = (
            "请根据盘中数据生成选股推荐。\n\n"
            f"=== 盘中数据 ===\n{context}\n"
            + (f"\n=== 历史反馈 ===\n{loop_context}\n" if loop_context else "")
            + "\n严格按 JSON 格式输出。"
        )

        messages = [{"role": "system", "content": INTRA_PICKS_PROMPT}]
        if chat_history:
            messages = messages + list(chat_history[-10:])
        messages.append({"role": "user", "content": user_msg})

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=3000,
            )
            raw = resp.choices[0].message.content
            result = self._parse_json(raw)
            if result and result.get("top_picks"):
                result["top_picks"] = self._hard_validate(
                    result["top_picks"], candidates
                )
            return result
        except Exception as e:
            logger.warning("Intraday Agent 失败: %s", e)
            return None

    # ── JSON 解析 ──────────────────────────────────────────────

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        try:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            data = json.loads(json_match.group() if json_match else raw)
        except json.JSONDecodeError:
            logger.warning("JSON 解析失败")
            return None

        # 填充缺失字段
        defaults = {
            "market_posture": "观望",
            "today_thesis": "信号不足，建议观望",
            "strategy": {"posture": "观望", "position_advice": "轻仓",
                         "primary_sectors": [], "avoid_sectors": []},
            "risk_alerts": [],
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return data

    # ── 硬校验 ──────────────────────────────────────────────────

    @staticmethod
    def _hard_validate(picks: list[dict], candidates: list[dict]) -> list[dict]:
        """规则引擎校验 Agent 输出，拦截幻觉。"""
        valid_codes = {c.get("code", "") for c in candidates}
        valid_signals = {c.get("code", ""): c.get("signal", "") for c in candidates}
        valid_pct = {c.get("code", ""): c.get("pct_chg", 0) for c in candidates}

        result = []
        for p in picks:
            code = p.get("code", "")
            # 过滤科创/北交/B股
            if code.startswith(("688", "8", "9")):
                continue
            # 过滤 LLM 幻觉
            if code and code not in valid_codes:
                logger.warning("硬校验剔除 %s: LLM幻觉", code)
                continue
            # 过滤已涨停
            if (valid_pct.get(code, 0) or 0) > 9.5:
                continue
            # 弱势信号降权
            signal = valid_signals.get(code, "")
            if signal in ("弱势回避", "高位风险"):
                p["confidence"] = max(1, (p.get("confidence") or 3) - 2)
                p["risk_note"] = (p.get("risk_note") or "") + " | 信号偏弱"
            # 主力流出降权
            for c in candidates:
                if c.get("code") == code and (c.get("net_main_ratio") or 0) < 0:
                    p["confidence"] = max(1, (p.get("confidence") or 3) - 1)
                    p["risk_note"] = (p.get("risk_note") or "") + " | 主力流出"
            result.append(p)

        # 板块集中度控制
        from collections import Counter
        sector_count = Counter()
        validated = []
        for p in result:
            s = p.get("sector", "")
            if sector_count.get(s, 0) >= 2:
                p["confidence"] = max(1, (p.get("confidence") or 3) - 1)
                p["risk_note"] = (p.get("risk_note") or "") + " | 板块集中"
            sector_count[s] = sector_count.get(s, 0) + 1
            validated.append(p)
        return validated


# ═══════════════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════════════

_agent: Optional[DecisionAgent] = None


def get_agent() -> DecisionAgent:
    global _agent
    if _agent is None:
        _agent = DecisionAgent()
    return _agent


def agent_brief_to_markdown(brief: dict) -> str:
    """盘前简报 JSON → Markdown（微信推送用）。"""
    today = datetime.now().strftime("%m/%d")
    lines = [f"== 盘前简报 {today} 09:25 ==", ""]

    st = brief.get("strategy", {})
    thesis = brief.get("today_thesis", "")
    posture = st.get("posture", "观望")
    position = st.get("position_advice", "轻仓")
    primary = "、".join(st.get("primary_sectors", []))
    avoid = "、".join(st.get("avoid_sectors", []))

    lines.append(f"【今日判断】{thesis}")
    lines.append(f"策略：{posture} | {position}")
    if primary:
        lines.append(f"主攻：{primary}")
    if avoid:
        lines.append(f"回避：{avoid}")
    lines.append("")

    oa = brief.get("overnight_analysis", {})
    if oa.get("overall_impact"):
        lines.append(f"【外盘】{oa['overall_impact']}")
        lines.append("")

    aa = brief.get("auction_analysis", {})
    if aa.get("breadth"):
        lines.append(f"【竞价】{aa['breadth']}")
        lines.append("")

    sw = brief.get("sector_watch", [])
    if sw:
        lines.append("【关注板块】")
        for s in sw[:6]:
            lines.append(f"  • {s.get('name','?')}：{s.get('attention_reason','')}")
        lines.append("")

    risks = brief.get("risk_alerts", [])
    reminder = st.get("key_reminder", "")
    if risks or reminder:
        lines.append(f"【风险】{reminder or risks[0]}")
        lines.append("")

    lines.append("-- 观澜 AI Agent --")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# ContextBuilder 静态方法（盘前）
# ═══════════════════════════════════════════════════════════════

@staticmethod
def _build_pre_market_context(indices, breadth, anomalies, hot_sectors):
    """构建盘前上下文。"""
    env = {"overnight_indices": {}, "auction_breadth": {}}
    for idx in indices:
        pct = idx.get("pct_chg")
        if pct is None:
            env["overnight_indices"][idx["name"]] = "数据不可用"
            continue
        if pct > 2: label = "大涨"
        elif pct > 0.5: label = "上涨"
        elif pct > 0.1: label = "微涨"
        elif pct > -0.1: label = "持平"
        elif pct > -0.5: label = "微跌"
        elif pct > -2: label = "下跌"
        else: label = "大跌"
        env["overnight_indices"][idx["name"]] = f"{label} ({pct:+.1f}%)"

    if breadth.get("status") != "no_data":
        env["auction_breadth"] = {
            "上涨占比": f"{int(breadth.get('up_ratio', 0) * 100)}%",
            "中位数涨跌": f"{breadth.get('median_pct', 0):+.1f}%",
        }

    anomaly_list = []
    for a in anomalies[:5]:
        entry = {"code": a.get("code",""), "name": a.get("name",""),
                 "pct": f"{a.get('pct_chg',0):+.1f}%",
                 "vol_ratio": f"{a.get('volume_ratio',0):.1f}x"}
        tags = []
        if a.get("gap", 0) > 5: tags.append("跳空高开")
        elif a.get("gap", 0) > 2: tags.append("显著高开")
        if a.get("volume_ratio", 0) > 5: tags.append("极度放量")
        if tags: entry["tags"] = "|".join(tags)
        anomaly_list.append(entry)

    sector_list = []
    for s in hot_sectors[:6]:
        net = s.get("net_main", 0)
        flow = "大幅流入" if net > 5 else ("流入" if net > 1 else ("持平" if net > -1 else "流出"))
        sector_list.append({
            "name": s.get("name",""),
            "pct": f"{s.get('pct_chg',0):+.1f}%",
            "fund_flow": f"{flow} {net:+.1f}亿",
        })

    ctx = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market_env": env,
        "anomalies": anomaly_list,
        "hot_sectors": sector_list,
    }
    return json.dumps(ctx, ensure_ascii=False, indent=2)


# 挂到 ContextBuilder 上
ContextBuilder.build_pre_market_context = staticmethod(_build_pre_market_context)


# ═══════════════════════════════════════════════════════════════
# CLI 测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    agent = DecisionAgent()
    print("Agent 初始化完成" if agent._init_client() else "Agent 不可用（无 API Key）")
