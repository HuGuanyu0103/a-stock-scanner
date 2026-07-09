#!/usr/bin/env python3
"""
AI 决策 Agent — 盘前简报 + 盘中推荐 + 持仓跟踪

架构：
  Structured Output Agent（单次 LLM 调用，JSON Schema 约束输出）
  输入：结构化信号 JSON（由 ContextBuilder 构建）
  输出：决策简报 JSON（含推荐 + 信号解释 + 策略建议）

设计原则：
  1. Agent 只做推理，不做计算 — 所有量化数据由规则引擎预计算
  2. Context Builder 去噪 — 只传 Top N 信号，不让 LLM 被信息淹没
  3. Fallback 优先 — LLM 调用失败时自动降级到模板模式，不中断产品链路

用法:
  from agent import DecisionAgent
  agent = DecisionAgent()
  brief = agent.generate_pre_market_brief(indices, breadth, anomalies, sectors)
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
# System Prompt — Agent 的决策框架（Step 3）
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个 A 股超短线交易决策顾问，专精 2-4 天持股周期。你的名字是「观澜」。

## 你的职责
你不是选股机器人。你是决策伙伴 — 帮助用户理解信号、看出矛盾、做出更好的交易决策。
你收到的所有量化数据已经由后端系统计算完毕，你不需要重新计算任何指标。

## 决策框架（必须严格按此顺序思考）

### Step 1: 先看大盘环境
分析隔夜外盘映射 + 竞价情绪 + 资金流向，判断今日是「进攻」「防守」还是「观望」。

### Step 2: 再看板块方向
哪些板块资金流入最强？哪些竞价阶段出现异动？如果板块层面找不到明确方向，
即使个股信号再强也不能积极推荐。

### Step 2.5: 重点关注个股
系统已经为你筛选了今日最值得关注的 3-5 只个股（已排除科创/创业板/ST，价格在 30 元以内，
小盘股已通过额外的量比和资金门槛验证）。你在生成推荐时必须优先评估这些个股。

### Step 3: 再看个股异动
竞价阶段出现跳空高开 + 量比异常放大的个股是超短线的重点观察对象。
分析这些异动背后的可能逻辑：是消息驱动？是板块带动？还是独立行情？

### Step 4: 给出策略建议
结合外盘、竞价、板块三个层面，给出今日操作策略：
- 追涨还是低吸？A 池为主还是 B 池为主？
- 哪些板块是今天的主攻方向？哪些需要回避？
- 仓位建议（满仓/半仓/轻仓/观望）

## 风险意识（最重要）
你有一条铁律：保护本金比追求收益更优先。当以下任一情况出现时，你必须明确警告用户：
- 竞价涨跌比显著偏空（上涨占比 < 40%）
- 隔夜外盘大幅下跌（美股主要指数跌幅 > 1.5%）
- 竞价无量（无明显异动个股 + 板块资金流向平淡）
- 市场轮动过快（资金在多个板块间快速切换，缺乏持续性）

## 输出格式
你必须严格按照以下 JSON 格式输出。不要添加 JSON 之外的任何文字。

## 排版要求
在生成 auction_analysis 和 sector_watch 时，遵循以下排版逻辑：
- 每个板块的第一条是「总述」——用一句判断性的话概括整体情况
- 后续是「分点」——具体的子项数据
- 不要罗列原始数据，给每个数据一个判断
- 竞价异动个股直接合并到 sector_watch 中，不单独列「竞价异动关注」区块
- 策略(今日判断、姿态、仓位、主攻板块)必须作为整个简报的第一段输出

```json
{
  "market_posture": "进攻 | 防守 | 观望",
  "market_posture_reason": "一句话解释为什么是这个判断",
  "today_thesis": "今天最核心的一个判断（30字以内），用户看完就知道今天该怎么做",
  "overnight_analysis": {
    "us_market": "美股隔夜表现的一句话总结",
    "hk_market": "港股表现的一句话总结",
    "overall_impact": "综合外盘对A股的情绪影响判断",
    "favored_sectors": ["外盘映射利好的板块"],
    "pressure_sectors": ["外盘映射承压的板块"]
  },
  "auction_analysis": {
    "breadth": "竞价涨跌比和市场广度的判断",
    "anomaly_highlights": [
      {
        "name": "股票名称",
        "code": "股票代码",
        "why_notable": "为什么这只值得关注（不是重复数据，是给出逻辑判断）"
      }
    ],
    "anomaly_theme": "异动股票的共同主题（如果看不出主题就说'无明显主题'）"
  },
  "sector_watch": [
    {
      "name": "板块名",
      "attention_reason": "关注理由（不是重复涨跌幅，是给判断）"
    }
  ],
  "strategy": {
    "posture": "进攻 | 防守 | 观望",
    "a_b_ratio": "A池X% + B池Y%",
    "position_advice": "满仓 | 半仓 | 轻仓 | 观望",
    "primary_sectors": ["主攻方向板块"],
    "avoid_sectors": ["建议回避板块"],
    "key_reminder": "今天最重要的一句提醒"
  },
  "risk_alerts": [
    "如果存在风险因素，按优先级列出"
  ]
}
```

## 风格要求
- 使用简洁、有力、有判断的中文。不要模棱两可。
- 每句话都应该是「判断」而不是「描述」。不说「外盘纳斯达克下跌0.8%」，
  要说「外盘偏冷，科技成长承压」。
- brevity is confidence — 短句比长句更有力。
"""


# ═══════════════════════════════════════════════════════════════
# Step 4: Context Builder — 结构化信号上下文
# ═══════════════════════════════════════════════════════════════



# ── 盘中选股 System Prompt ─────────────────────────────────

INTRA_SYSTEM_PROMPT = """你是一个 A 股超短线交易决策顾问「观澜」，正在盘中实时分析候选池。

## 你的任务
根据系统提供的盘中候选池数据，给出今日最值得关注的选股建议。

## 决策框架
1. 先看市场广度：上涨板块占比多少？市场是偏进攻还是偏防守？
2. 再看候选池结构：A 池（追涨）和 B 池（低吸）各有什么标的？哪个池质量更高？
3. 筛选 Top 3-5：优先选多信号共振的（A 池评分高 + 日评分也高 + 主力净占比大）
4. 给出操作建议：每只推荐的买入区间、止损位、目标位、仓位建议

## 冷启动模式（重要）
当你收到的「历史决策反馈」为空时，说明系统刚启动，还没有足够的历史数据。
此时你必须：
- 更依赖规则引擎的信号而非历史胜率
- 仓位建议偏向保守（轻仓/半仓，不输出满仓）
- 在 market_reminder 中提醒用户：「系统处于冷启动阶段，建议小额试仓积累数据」
- 不要编造历史胜率数据，诚实标注「暂无历史数据」

## 硬约束
- 不要推荐信号为「弱势回避」「高位风险」的股票
- 如果候选池整体质量不高（A 池最高分 < 5 且 B 池最高分 < 3），诚实告诉用户
- 每只推荐必须附带风险提示
- 如果市场广度 < 15%，必须建议防守

## 输出格式
严格 JSON：
{
  "market_read": "一句话判断当前市场状态",
  "quality_assessment": "候选池整体质量评价",
  "top_picks": [
    {
      "code": "股票代码",
      "name": "股票名称",
      "pool": "A|B",
      "sector": "所属板块",
      "confidence": 5,
      "reasoning": "为什么推荐（信号共振？资金确认？技术形态？）",
      "entry_zone": "买入区间",
      "stop_loss": "止损位",
      "target": "目标位",
      "position": "仓位建议",
      "risk_note": "该股特定风险"
    }
  ],
  "watchlist": [],
  "market_reminder": "当天最重要的一句提醒"
}
"""

# ── 多轮对话 System Prompt ─────────────────────────────────

CHAT_SYSTEM_PROMPT = """你是 A 股超短线交易决策顾问「观澜」。用户正在盘中交易，会向你追问个股和策略问题。

## 回答规则
- 回答简洁直接，每条回答不超过 3 句话
- 如果用户问的股票在候选池里，用盘中数据回答（评分、信号、资金面）
- 如果不在候选池里，用你的专业知识进行分析诊断（基本面、技术面、行业地位等），不要说「不在候选池所以无法诊断」
- 如果用户问「该买吗」，给出明确倾向但不替用户决策
- 如果市场风险高，主动提醒
- 诊断时给出具体的买入价位区间、目标价、止损位

## 禁止行为
- 不说「建议您自行判断」「无法诊断」「不在候选池」这类拒绝回答的话
- 不编造具体的盘中实时数据（如精确到小数点后的涨跌幅），但可以用专业判断给出合理分析
- 不要因为数据不足就直接拒绝，始终提供有价值的分析"""

class ContextBuilder:
    """将原始数据构建为 Agent 可理解的结构化上下文。

    核心原则：
    1. 去噪：只传 Top N，不让 LLM 被信息淹没
    2. 结构化：每个信号是「名称 + 数值 + 含义」三元组
    3. 标注异常：明显偏离正常范围的数据加警告标签
    """

    @staticmethod
    def build_pre_market_context(
        indices: list[dict],
        breadth: dict,
        anomalies: list[dict],
        hot_sectors: list[dict],
    ) -> str:
        """构建盘前简报的结构化上下文 JSON 字符串。"""
        context = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "market_environment": ContextBuilder._build_market_env(indices, breadth),
            "auction_anomalies": ContextBuilder._build_anomaly_list(anomalies),
            "hot_sectors": ContextBuilder._build_sector_list(hot_sectors),
        }
        return json.dumps(context, ensure_ascii=False, indent=2)

    @staticmethod
    def _build_market_env(indices: list[dict], breadth: dict) -> dict:
        """构建市场环境章节。"""
        env = {"overnight_indices": {}, "auction_breadth": {}}

        for idx in indices:
            pct = idx.get("pct_chg")
            if pct is None:
                env["overnight_indices"][idx["name"]] = "数据不可用"
                continue
            if pct > 2:
                label = "大涨"
            elif pct > 0.5:
                label = "上涨"
            elif pct > 0.1:
                label = "微涨"
            elif pct > -0.1:
                label = "持平"
            elif pct > -0.5:
                label = "微跌"
            elif pct > -2:
                label = "下跌"
            else:
                label = "大跌"
            env["overnight_indices"][idx["name"]] = f"{label} ({pct:+.1f}%)"

        if breadth.get("status") != "no_data":
            status_map = {"bullish": "偏乐观", "bearish": "偏悲观", "neutral": "中性"}
            env["auction_breadth"] = {
                "上涨家数": breadth.get("up_count", "?"),
                "下跌家数": breadth.get("down_count", "?"),
                "上涨占比": f"{int(breadth.get('up_ratio', 0) * 100)}%",
                "中位数涨跌": f"{breadth.get('median_pct', 0):+.1f}%",
                "情绪标签": status_map.get(breadth.get("status", ""), "未知"),
            }
        else:
            env["auction_breadth"] = {"状态": "竞价数据暂未就绪"}

        return env

    @staticmethod
    def _build_anomaly_list(anomalies: list[dict]) -> list[dict]:
        """构建竞价异动列表，附带含义标注。"""
        result = []
        for a in anomalies:
            entry = {
                "股票代码": a.get("code", ""),
                "名称": a.get("name", ""),
                "竞价涨幅": f"{a.get('pct_chg', 0):+.1f}%",
                "竞价量比": f"{a.get('volume_ratio', 0):.1f}x",
            }
            pct = a.get("pct_chg", 0)
            vol = a.get("volume_ratio", 0)
            gap = a.get("gap", 0)
            tags = []
            if gap > 5:
                tags.append("跳空高开幅度大")
            elif gap > 2:
                tags.append("显著高开")
            if vol > 10:
                tags.append("竞价极度放量")
            elif vol > 5:
                tags.append("竞价量能较强")
            elif vol > 2:
                tags.append("竞价温和放量")
            if pct > 8:
                tags.append("接近涨停竞价")
            if tags:
                entry["信号标注"] = " | ".join(tags)
            result.append(entry)
        return result[:5]

    @staticmethod
    def _build_sector_list(sectors: list[dict]) -> list[dict]:
        """构建板块列表，附带资金流向语义。"""
        result = []
        for s in sectors:
            entry = {
                "板块名称": s.get("name", ""),
                "竞价涨跌": f"{s.get('pct_chg', 0):+.1f}%",
            }
            net = s.get("net_main", 0)
            if net > 5:
                entry["主力资金"] = f"大幅流入 {net:+.1f}亿"
            elif net > 1:
                entry["主力资金"] = f"流入 {net:+.1f}亿"
            elif net > -1:
                entry["主力资金"] = f"基本持平 {net:+.1f}亿"
            else:
                entry["主力资金"] = f"流出 {net:+.1f}亿"
            result.append(entry)
        return result[:6]


# ═══════════════════════════════════════════════════════════════
# DecisionAgent — LLM 调用 + Fallback（Step 5）
# ═══════════════════════════════════════════════════════════════

class DecisionAgent:
    """AI 决策 Agent。

    使用方式:
        agent = DecisionAgent()
        brief = agent.generate_pre_market_brief(indices, breadth, anomalies, sectors)
        # brief 是 dict，若 LLM 不可用则返回 None（调用方应 fallback）
    """

    def __init__(self, model: str = "deepseek-chat"):
        self._model = model
        self._client = None
        self._api_available = None  # None = 未检测, True/False

    def _init_client(self) -> bool:
        """初始化 DeepSeek 客户端。返回是否可用。"""
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
            logger.info("未配置 DeepSeek API Key，Agent 降级到模板模式")
            self._api_available = False
            return False

        self._client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self._api_available = True
        return True

    @staticmethod
    def _get_api_key() -> Optional[str]:
        """获取 DeepSeek API Key。"""
        key = os.environ.get("DEEPSEEK_API_KEY")
        if key:
            return key
        config_file = DATA_DIR / "deepseek_key.txt"
        if config_file.exists():
            return config_file.read_text().strip()
        return None

    def generate_pre_market_brief(
        self,
        indices: list[dict],
        breadth: dict,
        anomalies: list[dict],
        hot_sectors: list[dict],
        key_stocks_context: str = "",
    ) -> Optional[dict]:
        """生成盘前决策简报。

        Returns:
            dict 或 None（None 表示 Agent 不可用，需 fallback）
        """
        if not self._init_client():
            return None

        context = ContextBuilder.build_pre_market_context(
            indices, breadth, anomalies, hot_sectors
        )

        user_message = (
            "请根据以下盘前数据，生成今日盘前决策简报。\n\n"
            "=== 结构化数据上下文 ===\n"
            f"{context}\n\n"
            "=== 你的任务 ===\n"
            "1. 先判断大盘环境（外盘 + 竞价情绪）\n"
            "2. 再分析板块方向（资金流入 + 异动主题）\n"
            "3. 然后评估竞价异动个股（有没有值得关注的？）\n"
            "4. 最后给出策略建议（进攻/防守/观望 + 主攻板块 + 回避板块 + 仓位）\n\n"
            "记住：你是「观澜」，做判断而不是描述。用简洁有力的中文。\n"
            "严格按照 JSON 格式输出。"
        )

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=3000,
            )
            raw = resp.choices[0].message.content
            tok = getattr(resp.usage, 'total_tokens', '?')
            logger.info("Agent LLM 调用成功: %s tokens", tok)
            result = self._parse_and_validate(raw)
            if result:
                # v7.1: 规则引擎硬校验 Agent 输出
                if result and result.get("top_picks"):
                    result["top_picks"] = self._hard_validate(
                        result["top_picks"], candidates)
                    result["top_picks_before_validation"] = len(result.get("top_picks", []))
                # v7.2: 注入确定性仓位计算
                if result and result.get("strategy"):
                    try:
                        from loop_analyzer import get_loop_analyzer
                        la = get_loop_analyzer()
                        ctx = la.get_agent_context_injection()
                        pos = self._calculate_position(
                            breadth, 
                            result.get("strategy", {}).get("sentiment_index"),
                            ctx.get("open_count", 0),
                            ctx.get("has_history", False)
                        )
                        result["strategy"]["position_calc"] = pos
                    except Exception:
                        pass
                return result
        except Exception as e:
            logger.warning("Agent LLM 调用失败: %s", e)

        return None

    def _parse_and_validate(self, raw: str) -> Optional[dict]:
        """解析 LLM 输出并校验必填字段。"""
        try:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Agent 输出 JSON 解析失败: %s", e)
            logger.debug("原始输出前500字: %s", raw[:500])
            return None

        # 校验并填充缺失字段
        defaults = {
            "market_posture": "观望",
            "market_posture_reason": "Agent 输出不完整",
            "today_thesis": "盘前信号不足，建议观望",
            "overnight_analysis": {},
            "auction_analysis": {},
            "sector_watch": [],
            "strategy": {
                "posture": "观望",
                "a_b_ratio": "A池50% + B池50%",
                "position_advice": "轻仓",
                "primary_sectors": [],
                "avoid_sectors": [],
                "key_reminder": "等待盘中确认",
            },
            "risk_alerts": ["Agent 输出不完整，建议人工判断"],
        }
        for key, default in defaults.items():
            if key not in data:
                data[key] = default

        # 硬约束：移除不应该推荐的板块
        if "strategy" in data and isinstance(data["strategy"], dict):
            ban_kw = ("科创", "北交", "新三板")
            for field in ("primary_sectors", "avoid_sectors"):
                if field in data["strategy"]:
                    data["strategy"][field] = [
                        s for s in data["strategy"][field]
                        if not any(kw in str(s) for kw in ban_kw)
                    ]

        return data



# ──────────────────────────────────────────────────────────────
# 盘中上下文构建
# ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_intraday_context(candidates: list[dict],
                                 hot_sectors: list[str],
                                 signals: dict,
                                 breadth: float = 0.5) -> str:
        """构建盘中选股的结构化上下文。

        只取 Top 10 候选（A池5 + B池5），每条附带：
        - 评分（盘中 + 日评）
        - 信号（技术面 + 资金面）
        - 板块归属
        - 风险标注
        """
        top_a = [c for c in candidates if c.get("pool") == "A"][:5]
        top_b = [c for c in candidates if c.get("pool") == "B"][:5]

        ctx = {
            "generated_at": __import__("datetime").datetime.now().strftime("%H:%M:%S"),
            "market_snapshot": {
                "上涨板块占比": f"{int(breadth * 100)}%",
                "热板块": hot_sectors[:8],
            },
            "top_picks_a": [
                ContextBuilder._stock_summary(s, "A") for s in top_a
            ],
            "top_picks_b": [
                ContextBuilder._stock_summary(s, "B") for s in top_b
            ],
        }

        sent_data = signals.get("sentiment", {}).get("data", {})
        market = sent_data.get("market", {}) if sent_data else {}
        if market:
            ctx["market_signal"] = {
                "情绪指数": market.get("sentiment_index", "?"),
                "炸板率": f"{market.get('po_ban_rate', 0) * 100:.0f}%",
                "轮动速度": market.get("rotation_speed", "?"),
            }

        return __import__("json").dumps(ctx, ensure_ascii=False, indent=2)

    @staticmethod
    def _stock_summary(s: dict, pool: str) -> dict:
        """单只股票的上下文摘要。"""
        tag = "A池-追涨" if pool == "A" else "B池-低吸"
        summary = {
            "代码": s.get("code", ""),
            "名称": s.get("name", ""),
            "池": tag,
            "板块": s.get("sector", ""),
            "盘中评分": s.get("score", 0),
        }
        ds = s.get("daily_combined_score")
        if ds is not None:
            summary["日评分"] = ds
        pct = s.get("pct_chg")
        if pct is not None:
            pct_str = f"{pct:+.1f}%"
            summary["涨跌幅"] = pct_str
        summary["信号"] = s.get("signal", "?")
        summary["量比"] = s.get("volume_ratio", 0)
        summary["主力净占比"] = f"{s.get('net_main_ratio', 0):+.1f}%"
        summary["日内位置"] = f"{s.get('intraday_position', 0):.2f}"
        return summary

# ──────────────────────────────────────────────────────────────
# 盘中选股推荐
# ──────────────────────────────────────────────────────────────

    def generate_intraday_picks(
        self,
        candidates: list[dict],
        hot_sectors: list[str],
        signals: dict,
        breadth: float = 0.5,
        chat_history: list[dict] | None = None,
    ) -> dict | None:
        """生成盘中选股推荐。

        Args:
            candidates: 候选池股票列表（含 A 池 + B 池）
            hot_sectors: 热板块名称列表
            signals: SignalStore.get_all() 的返回值
            breadth: 市场广度（上涨板块占比）
            chat_history: 可选的多轮对话历史

        Returns:
            dict 或 None
        """
        if not self._init_client():
            return None

        context = self._build_intraday_context(
            candidates, hot_sectors, signals, breadth
        )

        user_message = (
            "请根据以下盘中数据，生成盘中选股决策建议。\n\n"
            "=== 盘中数据上下文 ===\n"
            f"{context}\n\n"
            "=== 你的任务 ===\n"
            "1. 判断当前市场情绪和市场广度\n"
            "2. 从候选池中选出最值得关注的 3-5 只股票\n"
            "3. 每只推荐必须说明理由（信号共振？资金确认？）\n"
            "4. 给出每只的买入区间、止损位、目标位\n"
            "5. 如果候选池质量不高，诚实地说\n\n"
            "记住：你是「观澜」。做判断而不是描述。简洁有力。\n"
            "严格按照 JSON 格式输出。"
        )

        messages = [
            {"role": "system", "content": INTRA_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        if chat_history:
            messages = [messages[0]] + chat_history + [messages[1]]

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=3000,
            )
            raw = resp.choices[0].message.content
            tok = getattr(resp.usage, 'total_tokens', '?')
            logger.info("Intraday Agent: %s tokens", tok)
            result = self._parse_and_validate(raw)
            if result:
                # v7.1: 规则引擎硬校验 Agent 输出
                if result and result.get("top_picks"):
                    result["top_picks"] = self._hard_validate(
                        result["top_picks"], candidates)
                    result["top_picks_before_validation"] = len(result.get("top_picks", []))
                # v7.2: 注入确定性仓位计算
                if result and result.get("strategy"):
                    try:
                        from loop_analyzer import get_loop_analyzer
                        la = get_loop_analyzer()
                        ctx = la.get_agent_context_injection()
                        pos = self._calculate_position(
                            breadth, 
                            result.get("strategy", {}).get("sentiment_index"),
                            ctx.get("open_count", 0),
                            ctx.get("has_history", False)
                        )
                        result["strategy"]["position_calc"] = pos
                    except Exception:
                        pass
                return result
        except Exception as e:
            logger.warning("Intraday Agent 调用失败: %s", e)

        return None

# ──────────────────────────────────────────────────────────────
# 多轮对话
# ──────────────────────────────────────────────────────────────

    def chat(
        self,
        user_message: str,
        candidates: list[dict],
        hot_sectors: list[str],
        signals: dict,
        chat_history: list[dict] | None = None,
        stock_context: str = "",
    ) -> str | None:
        """多轮对话：用户可以追问 Agent。

        Args:
            user_message: 用户输入
            candidates: 当前候选池
            hot_sectors: 热板块
            signals: 三系统信号
            chat_history: 历史对话
            stock_context: 用户消息中提到的个股实时分析数据

        Returns:
            Agent 的文本回复，或 None
        """
        if not self._init_client():
            return None

        context = self._build_intraday_context(
            candidates, hot_sectors, signals, 0.5
        )

        system_msg = (
            CHAT_SYSTEM_PROMPT + "\n\n"
            "=== 当前盘中数据（仅候选池，用户可能问其他股票）===\n" + context
        )
        if stock_context:
            system_msg += "\n\n=== 用户询问的股票实时数据（脚本获取，非LLM编造）===\n" + stock_context
            system_msg += "\n请基于以上实时数据进行分析诊断，给出买卖建议、入场价位、目标价和止损位。"

        system_msg += "\n\n"
        system_msg += (
            "重要提醒：上面候选池只是部分数据。用户可能询问任何A股股票。"
            "如果提供了该股的实时数据，请基于数据回答。如果没有实时数据，用你的专业知识分析。"
            "永远不要说'不在候选池'或'无法诊断'。给出具体的诊断建议。"
        )

        messages = [{"role": "system", "content": system_msg}]
        if chat_history:
            messages.extend(chat_history)
        messages.append({"role": "user", "content": user_message})

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.6,
                max_tokens=1500,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning("Chat 调用失败: %s", e)
            return None


    # ── 盘中上下文 ───────────────────────────────────────────

    @staticmethod
    def _build_intraday_context(candidates, hot_sectors, signals, breadth=0.5):
        """构建盘中选股结构化上下文，只取 Top 10。"""
        import json as _json
        from datetime import datetime as _dt
        top_a = [c for c in candidates if c.get("pool") == "A"][:5]
        top_b = [c for c in candidates if c.get("pool") == "B"][:5]

        def _stock_summary(s, pool_tag):
            return {
                "代码": s.get("code", ""),
                "名称": s.get("name", ""),
                "池": pool_tag,
                "板块": s.get("sector", ""),
                "盘中评分": s.get("score", 0),
                "日评分": s.get("daily_combined_score"),
                "涨跌幅": f"{s.get('pct_chg', 0):+.1f}%" if s.get("pct_chg") is not None else "?",
                "信号": s.get("signal", "?"),
                "量比": s.get("volume_ratio", 0),
                "主力净占比": f"{s.get('net_main_ratio', 0):+.1f}%",
                "日内位置": f"{s.get('intraday_position', 0):.2f}",
            }

        ctx = {
            "generated_at": _dt.now().strftime("%H:%M:%S"),
            "market_snapshot": {
                "上涨板块占比": f"{int(breadth * 100)}%",
                "热板块": hot_sectors[:8] if hot_sectors else [],
            },
            "top_picks_a": [_stock_summary(s, "A池-追涨") for s in top_a],
            "top_picks_b": [_stock_summary(s, "B池-低吸") for s in top_b],
        }

        sent_data = signals.get("sentiment", {}).get("data", {})
        market = sent_data.get("market", {})
        if market:
            ctx["market_signal"] = {
                "情绪指数": market.get("sentiment_index", "?"),
                "炸板率": f"{market.get('po_ban_rate', 0) * 100:.0f}%" if market.get("po_ban_rate") else "?",
                "轮动速度": market.get("rotation_speed", "?"),
            }
        return _json.dumps(ctx, ensure_ascii=False, indent=2)

    # ── 盘中选股 ─────────────────────────────────────────────

    def generate_intraday_picks(self, candidates, hot_sectors, signals, breadth=0.5, chat_history=None, loop_context=""):
        """生成盘中选股推荐。"""
        if not self._init_client():
            return None

        context = self._build_intraday_context(candidates, hot_sectors, signals, breadth)

        user_message = (
            "请根据以下盘中数据，生成盘中选股决策建议。\n\n"
            "=== 盘中数据上下文 ===\n"
            + (("\n\n" + key_stocks_context + "\n\n") if key_stocks_context else "")
                + context + "\n\n=== 历史反馈数据 ===\n" + loop_context + "\n\n=== \n\n"
            "=== 你的任务 ===\n"
            "1. 判断当前市场情绪和市场广度\n"
            "2. 从候选池中选出最值得关注的 3-5 只股票\n"
            "3. 每只推荐必须说明理由（信号共振？资金确认？）\n"
            "4. 给出每只的买入区间、止损位、目标位\n"
            "5. 如果候选池质量不高，诚实地说\n\n"
            "记住：你是「观澜」。做判断而不是描述。简洁有力。\n"
            "严格按照 JSON 格式输出。"
        )

        messages = [
            {"role": "system", "content": INTRA_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        if chat_history:
            messages = [messages[0]] + list(chat_history) + [messages[1]]

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=3000,
            )
            raw = resp.choices[0].message.content
            tok = getattr(resp.usage, "total_tokens", "?")
            logger.info("Intraday Agent: %s tokens", tok)
            result = self._parse_and_validate(raw)
            if result:
                # v7.1: 规则引擎硬校验 Agent 输出
                if result and result.get("top_picks"):
                    result["top_picks"] = self._hard_validate(
                        result["top_picks"], candidates)
                    result["top_picks_before_validation"] = len(result.get("top_picks", []))
                # v7.2: 注入确定性仓位计算
                if result and result.get("strategy"):
                    try:
                        from loop_analyzer import get_loop_analyzer
                        la = get_loop_analyzer()
                        ctx = la.get_agent_context_injection()
                        pos = self._calculate_position(
                            breadth, 
                            result.get("strategy", {}).get("sentiment_index"),
                            ctx.get("open_count", 0),
                            ctx.get("has_history", False)
                        )
                        result["strategy"]["position_calc"] = pos
                    except Exception:
                        pass
                return result
        except Exception as e:
            logger.warning("Intraday Agent 调用失败: %s", e)
        return None

    # ── 多轮对话 ─────────────────────────────────────────────

    def chat(self, user_message, candidates, hot_sectors, signals, chat_history=None):
        """多轮对话：用户追问 Agent。"""
        if not self._init_client():
            return None

        context = self._build_intraday_context(candidates, hot_sectors, signals, 0.5)
        system_msg = CHAT_SYSTEM_PROMPT + "\n\n=== 当前盘中数据 ===\n" + context

        messages = [{"role": "system", "content": system_msg}]
        if chat_history:
            messages.extend(list(chat_history))
        messages.append({"role": "user", "content": user_message})

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.6,
                max_tokens=1500,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning("Chat 调用失败: %s", e)
            return None



    @staticmethod
    def _hard_validate(picks: list[dict], candidates: list[dict]) -> list[dict]:
        """规则引擎硬校验层：Agent 输出后过风控过滤。

        解决评审反馈 P1 #12：「Agent 输出后增加规则引擎硬校验层，拦截 AI 幻觉」。

        校验规则：
        1. 科创板(688)/北交所(8/9) → 直接剔除
        2. 信号含「弱势回避」「高位风险」→ 降为 watchlist
        3. 候选池中不存在的股票 → 剔除（防止 LLM 编造代码）
        4. 主力净占比为负 → 降权，标注风险
        5. 涨停板已封 → 剔除（信号为接近涨停竞价）

        Returns:
            过滤后的 picks 列表
        """
        valid_codes = {c.get("code", "") for c in candidates}
        valid_signals = {c.get("code", ""): c.get("signal", "") for c in candidates}
        valid_pct = {c.get("code", ""): c.get("pct_chg", 0) for c in candidates}

        result = []
        for p in picks:
            code = p.get("code", "")

            # 规则1: 科创板/北交所
            if code.startswith(("688", "8", "9")):
                logger.info("硬校验剔除 %s: 科创板/北交所", code)
                continue

            # 规则3: LLM 幻觉（编造了候选池中不存在的股票代码）
            if code and code not in valid_codes:
                logger.warning("硬校验剔除 %s: LLM 编造的代码", code)
                continue

            # 规则2: 弱势信号
            signal = valid_signals.get(code, "")
            if signal in ("弱势回避", "高位风险"):
                p["confidence"] = max(1, (p.get("confidence") or 3) - 2)
                p["risk_note"] = (p.get("risk_note") or "") + " | 信号偏弱，仅作观察"

            # 规则4: 主力净占比为负
            # Check candidate data for net_main_ratio
            for c in candidates:
                if c.get("code") == code and (c.get("net_main_ratio") or 0) < 0:
                    p["confidence"] = max(1, (p.get("confidence") or 3) - 1)
                    p["risk_note"] = (p.get("risk_note") or "") + " | 主力资金流出"

            # 规则5: 接近涨停
            pct = valid_pct.get(code, 0) or 0
            if pct > 9.5:
                logger.info("硬校验剔除 %s: 接近涨停已封", code)
                continue

            result.append(p)

        # 规则6: Top 3 板块分散 — 同一板块不超过 2 只
        from collections import Counter
        sector_count = Counter()
        validated = []
        for p in result:
            s = p.get("sector", "")
            if sector_count.get(s, 0) >= 2:
                p["confidence"] = max(1, (p.get("confidence") or 3) - 1)
                p["risk_note"] = (p.get("risk_note") or "") + " | 板块已有多只入选，注意集中度"
            sector_count[s] = sector_count.get(s, 0) + 1
            validated.append(p)
        result = validated

        return result

    @staticmethod
    def _calculate_position(market_breadth, sentiment_index, open_count, has_history=False):
        """确定性仓位计算。
        
        输入：
        - market_breadth: 上涨板块占比 (0-1)
        - sentiment_index: 情绪指数 (0-100)
        - open_count: 当前持仓数
        - has_history: 是否有历史胜率数据
        
        输出：仓位建议（ratio 0-1）和语义标签
        
        逻辑：
        - 基础仓位 = 市场广度 × 情绪修正
        - 持仓数修正：已有 3 只以上 → 仓位减半
        - 冷启动修正：无历史数据 → 仓位上限 0.5
        """
        # 基础仓位 = 市场广度映射
        base = min(1.0, max(0.15, market_breadth * 1.2))
        
        # 情绪修正
        if sentiment_index and sentiment_index > 80:
            base *= 0.7  # 情绪过热，防追高
        elif sentiment_index and sentiment_index < 30:
            base *= 0.5  # 情绪冰点，防恐慌
        
        # 持仓数修正
        if open_count >= 5:
            base *= 0.3
        elif open_count >= 3:
            base *= 0.5
        elif open_count >= 1:
            base *= 0.8
        
        # 冷启动修正
        if not has_history:
            base = min(base, 0.5)
        
        base = round(base, 2)
        
        if base >= 0.8:
            label = "可满仓"
        elif base >= 0.5:
            label = "半仓"
        elif base >= 0.3:
            label = "轻仓"
        else:
            label = "观望"
        
        return {"ratio": base, "label": label, "factors": {
            "breadth": round(market_breadth, 2),
            "sentiment": sentiment_index,
            "open_count": open_count,
            "cold_start": not has_history,
        }}

# ═══════════════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════════════

_agent: Optional[DecisionAgent] = None


def get_agent() -> DecisionAgent:
    """获取 Agent 单例。"""
    global _agent
    if _agent is None:
        _agent = DecisionAgent()
    return _agent


def agent_brief_to_markdown(brief: dict) -> str:
    """将 Agent 输出的 JSON 转换为 Markdown（供微信推送）。

    v7.3 版式：判断+策略置顶 → 外盘 → 情绪 → 板块 → 个股 → 风险
    """
    today = datetime.now().strftime("%m/%d")
    lines = [f"== 盘前简报 {today} 09:25 ==", ""]

    # ── 判断 + 策略（置顶）───────────────────────────────
    st = brief.get("strategy", {})
    thesis = brief.get("today_thesis", "")
    posture = st.get("posture", "")
    ratio = st.get("a_b_ratio", "")
    position = st.get("position_advice", "")
    primary = "、".join(st.get("primary_sectors", []))
    avoid = "、".join(st.get("avoid_sectors", []))
    lines.append(f"【今日判断】{thesis}")
    parts = [posture, ratio, position]
    strat_line = "  ".join(p for p in parts if p)
    lines.append(f"策略：{strat_line}")
    if primary:
        lines.append(f"主攻：{primary}")
    if avoid:
        lines.append(f"回避：{avoid}")
    lines.append("")

    # ── 外盘 ──────────────────────────────────────────────
    oa = brief.get("overnight_analysis", {})
    if oa:
        impact = oa.get("overall_impact", "")
        lines.append(f"【外盘】{impact}")
        if oa.get("us_market"):
            lines.append(f"  • 美股：{oa['us_market']}")
        if oa.get("hk_market"):
            lines.append(f"  • 港股：{oa['hk_market']}")
        lines.append("")

    # ── 竞价情绪 ─────────────────────────────────────────
    aa = brief.get("auction_analysis", {})
    if aa:
        theme = aa.get("anomaly_theme", "")
        breadth = aa.get("breadth", "")
        header = theme or breadth or ""
        lines.append(f"【竞价情绪】{header}")
        if breadth and breadth != theme:
            lines.append(f"  {breadth}")
        lines.append("")

    # ── 重点关注板块 ─────────────────────────────────────
    sw = brief.get("sector_watch", [])
    if sw:
        top_theme = sw[0].get("attention_reason", "") if sw else ""
        lines.append(f"【关注板块】{top_theme}")
        for s in sw[:6]:
            reason = s.get("attention_reason", "")
            if reason == top_theme:
                continue
            lines.append(f"  • {s.get('name','?')}：{reason}")
        lines.append("")

    # ── 重点关注个股 ─────────────────────────────────────
    highlights = aa.get("anomaly_highlights", [])
    top_picks = brief.get("top_picks", [])
    key_stocks = highlights + [
        {"name": p.get("name","?"), "code": p.get("code",""),
         "why_notable": p.get("reasoning",""),
         "sector": p.get("sector","")}
        for p in top_picks[:3]
    ]
    if key_stocks:
        lines.append("【关注个股】")
        seen = set()
        for ks in key_stocks[:5]:
            name = ks.get("name", "?")
            code = ks.get("code", "")
            if code in seen:
                continue
            seen.add(code)
            why = ks.get("why_notable", "")
            sector = ks.get("sector", "")
            sector_tag = f"({sector})" if sector else ""
            lines.append(f"  • {name} {code} {sector_tag}：{why}")
        lines.append("")

    # ── 风险提示 ─────────────────────────────────────────
    risks = brief.get("risk_alerts", [])
    reminder = st.get("key_reminder", "")
    risk_header = reminder if reminder else (risks[0] if risks else "")
    if risks or reminder:
        risk_header = reminder if reminder else (risks[0] if risks else "")
        lines.append(f"【风险】{risk_header}")
        for r in (risks[1:] if reminder and risks else risks):
            lines.append(f"  • {r}")
        lines.append("")

    lines.append("-- 由 AI 决策 Agent [观澜] 生成 --")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI 测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    mock_indices = [
        {"name": "纳斯达克", "pct_chg": 1.2},
        {"name": "标普500", "pct_chg": 0.5},
        {"name": "道指", "pct_chg": -0.3},
        {"name": "恒生指数", "pct_chg": 0.8},
    ]
    mock_breadth = {
        "total": 4500, "up_count": 2800, "down_count": 1500,
        "up_ratio": 0.62, "median_pct": 0.4, "status": "bullish",
    }
    mock_anomalies = [
        {"code": "000063", "name": "中兴通讯", "pct_chg": 5.2, "volume_ratio": 8.5, "gap": 3.2},
        {"code": "600519", "name": "贵州茅台", "pct_chg": 1.5, "volume_ratio": 4.2, "gap": 0.8},
        {"code": "300750", "name": "宁德时代", "pct_chg": 3.8, "volume_ratio": 6.0, "gap": 2.1},
    ]
    mock_sectors = [
        {"name": "光通信模块", "pct_chg": 2.1, "net_main": 8.5},
        {"name": "AI芯片", "pct_chg": 1.8, "net_main": 6.2},
        {"name": "新能源车", "pct_chg": -0.5, "net_main": -1.2},
    ]

    print("=" * 60)
    print("  Agent 测试 - 盘前决策简报")
    print("=" * 60)
    print()

    agent = DecisionAgent()
    brief = agent.generate_pre_market_brief(
        mock_indices, mock_breadth, mock_anomalies, mock_sectors
    )

    if brief:
        print("[OK] Agent 输出：")
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        print()
        print("-- Markdown 格式 --")
        print(agent_brief_to_markdown(brief))
    else:
        print("[WARN] Agent 不可用（无 API Key 或 openai 未安装）")
        print("  -> 降级到模板模式，不影响产品正常使用")
        print()
        print("-- Context Builder 输出（验证上下文正确性）--")
        ctx = ContextBuilder.build_pre_market_context(
            mock_indices, mock_breadth, mock_anomalies, mock_sectors
        )
        print(ctx)
