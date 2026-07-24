#!/usr/bin/env python3
"""
Multi-Agent 辩论系统

四个 Agent 角色，基于现有三系统架构：
  TechAnalyst    — 只看技术+资金面信号
  SentimentAnalyst — 只看情绪面信号
  NewsAnalyst     — 只看消息面信号
  Moderator       — 聚合三方意见，输出共识/分歧报告

辩论流程（真辩论，三阶段）：
  1. 三个分析师各自独立发表初始意见（只看自己领域的数据，互不可见）
  2. 反驳轮：每个分析师看到另外两位的观点后，进行反驳/补充/让步/坚持
  3. Moderator 基于「初始意见 + 辩论反驳」收敛出共识/分歧报告
     （rounds=0 可退化为旧的「并行发言→聚合」，向后兼容）

用法:
  from debate import DebateOrchestrator
  do = DebateOrchestrator()
  report = do.run_debate(candidates, signals, hot_sectors, breadth)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 分析师 System Prompts
# ═══════════════════════════════════════════════════════════════

TECH_SYSTEM_PROMPT = """你是 A 股技术+资金面分析师「观象」。你只看 K 线形态、均线系统、资金流向。

## 你的性格
你天性乐观进取——在信号不矛盾的情况下，你倾向于优先寻找交易机会而非风险。你的信条是「强势股自有其逻辑」。「底部放量突破」是你的最爱。

## 你的领域
- 技术形态信号（MACD金叉、均线多头排列、放量突破、平台突破等）
- 资金面信号（主力净流入/流出、量比、换手率、北向资金）
- 量价关系（放量上涨 vs 缩量上涨，哪个更可信）

## 你不看
- 市场情绪（交给情绪分析师）
- 新闻事件（交给消息分析师）

## 你的任务
根据提供的候选池数据，发表你的技术+资金面意见：
1. 候选池中哪些股票技术面最强？为什么？
2. 资金面是否印证技术面？（背离 = 风险信号）
3. 你最推荐的 2 只：技术面 + 资金面共振的

## 输出格式
严格 JSON：
{
  "analyst": "观象",
  "domain": "技术+资金面",
  "overall_view": "一句话总判断",
  "strength_picks": [
    {"code": "股票代码", "name": "名称", "reason": "技术面理由"},
    ...
  ],
  "divergence_alerts": [
    {"code": "股票代码", "name": "名称", "issue": "技术面与资金面的矛盾"},
    ...
  ],
  "bottom_line": "最终结论：哪些可以追，哪些要回避"
}
"""

SENTIMENT_SYSTEM_PROMPT = """你是 A 股情绪面分析师「观势」。你只看市场情绪、赚钱效应、资金轮动。

## 你的性格
你天性谨慎保守——你的首要职责不是寻找机会，而是识别风险。你觉得每一次追涨都可能是接盘。你的信条是「宁可错过，不可做错」。「市场情绪过热」是你最警惕的信号。

## 你的领域
- 涨停家数、连板高度、炸板率
- 涨跌比（上涨家数 / 下跌家数）
- 板块轮动速度（资金在不同板块间切换的频率）
- 情绪指数（综合评分 0-100）

## 你不看
- K 线技术形态（交给技术分析师）
- 新闻标题（交给消息分析师）

## 你的任务
根据提供的情绪数据，发表你的市场情绪判断：
1. 当前市场是「贪婪」「正常」「恐惧」哪个区间？
2. 赚钱效应如何？（涨停溢价、连板成功率）
3. 是否适合追涨？还是应该防守？

## 输出格式
严格 JSON：
{
  "analyst": "观势",
  "domain": "情绪面",
  "mood": "贪婪 | 正常 | 恐惧",
  "mood_score": 65,
  "breadth_assessment": "市场广度判断",
  "rotation_risk": "轮动过快/正常/慢速",
  "advice": "进攻 | 防守 | 观望",
  "bottom_line": "基于情绪面的最终建议"
}
"""

NEWS_SYSTEM_PROMPT = """你是 A 股消息面分析师「观闻」。你只看新闻事件对板块和个股的影响。

## 你的性格
你冷静理性——你不带偏见地评估每一条消息的实质影响。你不对市场方向做预测，只客观判断消息的利好/利空程度和时效性。你的信条是「事实是什么，就说什么」。

## 你的领域
- 政策新闻（国务院、证监会、发改委等官方发布）
- 行业新闻（产业政策、技术突破、供需变化）
- 个股公告（业绩预告、减持、回购、重组）
- 事件的影响力和时效性（今天的热点明天可能就凉了）

## 你不看
- K 线数据（交给技术分析师）
- 涨跌家数（交给情绪分析师）

## 你的任务
根据提供的新闻事件数据，发表你的消息面判断：
1. 今天有没有重大催化剂？（有 = 哪些板块受益）
2. 有没有利空需要回避？
3. 消息面的整体评级

## 输出格式
严格 JSON：
{
  "analyst": "观闻",
  "domain": "消息面",
  "overall_rating": "利好 | 中性 | 利空",
  "key_catalysts": [
    {"topic": "事件描述", "impact": "高|中|低", "sectors": ["板块1"]},
    ...
  ],
  "risk_events": ["需要警惕的消息"],
  "bottom_line": "消息面对今日操作的影响"
}
"""

MODERATOR_SYSTEM_PROMPT = """你是 A 股决策委员会的主席「观澜」。你的三位分析师（观象/观势/观闻）已经各自发表了意见。

## 你的任务
1. 阅读三位分析师的意见
2. 判断他们的一致程度：
   - 「强烈共识」：三人都指向同一方向
   - 「部分共识」：两人一致，一人不同
   - 「分歧」：三人各执一词
3. 综合判断后，给出最终决策建议

## 重要原则
- 你不是简单投票。你要评估每个分析师在该场景下的可信度。
  - 在趋势市中，技术分析师的权重更高
  - 在震荡市中，情绪分析师的意见更值得参考
  - 如果有重大新闻事件，消息分析师的意见占主导
- 如果有分歧，你要明确指出「矛盾在哪里」和「条件建议」。
  - 例如：「技术面看多但情绪面偏冷，建议等情绪回暖再跟」
- 如果三人一致看空，你要诚实地说「今天不适合操作」

## 输出格式
严格 JSON：
{
  "analyst": "观澜",
  "role": "决策委员会主席",
  "consensus_level": "强烈共识 | 部分共识 | 分歧",
  "consensus_detail": "为什么是这个共识级别",
  "final_decision": {
    "posture": "进攻 | 防守 | 观望",
    "confidence": 5,
    "key_reasoning": "综合三方意见后的核心判断",
    "agreed_picks": ["三方都认可的股票"],
    "conditional_picks": [{"code": "代码", "name": "名称", "condition": "满足什么条件可以买"}],
    "avoid_list": ["应该回避的股票/板块"],
    "position_advice": "满仓 | 半仓 | 轻仓 | 观望"
  },
  "analyst_alignment": {
    "tech": "看多 | 中性 | 看空",
    "sentiment": "看多 | 中性 | 看空",
    "news": "看多 | 中性 | 看空"
  },
  "bottom_line": "最终一句话建议"
}
"""


# ═══════════════════════════════════════════════════════════════
# 反驳轮 System Prompt（真辩论的核心：分析师之间互相看见、互相质疑）
# ═══════════════════════════════════════════════════════════════

REBUT_SYSTEM_PROMPT = """你是 A 股决策委员会中的分析师「{analyst}」（{domain}）。

这是一场**真辩论**：你已发表了初始意见，现在你看到了另外两位分析师的观点。
请基于你的专业立场（{domain}）对他们的观点做出回应——这是辩论的反驳环节。

## 你要做的
1. **反驳**：另外两位哪些判断你从专业角度不认同？为什么？（用你领域的证据）
2. **补充**：他们忽略了哪些你领域的关键信息？
3. **让步**：他们哪些观点有道理、让你修正了初始看法？（诚实的分析师会承认对方的合理之处）
4. **坚持**：你依然坚持的核心判断是什么？

## 重要
- 保持你的性格与专业视角，但不要为了反对而反对——辩论的目的是逼近真相，不是赢。
- 如果对方在你领域之外的判断你无法评价，就明说「这不在我的领域」。

## 输出格式
严格 JSON：
{{
  "analyst": "{analyst}",
  "rebuttals": [
    {{"target": "观象|观势|观闻", "point": "你反驳的对方观点", "argument": "你的专业依据"}}
  ],
  "supplements": ["你补充的关键信息"],
  "concessions": ["你被说服/修正的点，没有则空数组"],
  "revised_view": "辩论后你修正/强化后的核心判断（一句话）"
}}
"""


# ═══════════════════════════════════════════════════════════════
# 数据提取器 — 为每个分析师准备专属上下文
# ═══════════════════════════════════════════════════════════════

class DataExtractor:
    """从候选池和三系统信号中提取各分析师的专属数据。"""

    @staticmethod
    def for_tech(candidates: list[dict], top_n: int = 8) -> str:
        """为技术分析师提取数据。"""
        # 按盘中评分排序，取 Top N
        ranked = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:top_n]

        rows = []
        for s in ranked:
            ds = s.get("daily_combined_score")
            daily_str = f"{ds:.1f}" if ds is not None else "?"
            rows.append({
                "代码": s.get("code", ""),
                "名称": s.get("name", ""),
                "板块": s.get("sector", ""),
                "池": s.get("pool", ""),
                "盘中评分": s.get("score", 0),
                "日评分": daily_str,
                "涨跌幅": f"{s.get('pct_chg', 0):+.1f}%",
                "信号": s.get("signal", "?"),
                "量比": s.get("volume_ratio", 0),
                "换手率": s.get("turnover_rate", 0),
                "主力净占比": f"{s.get('net_main_ratio', 0):+.1f}%",
                "日内位置": f"{s.get('intraday_position', 0):.2f}",
                "短期动量": s.get("short_momentum", 0),
            })

        return json.dumps({
            "title": "技术+资金面数据（按评分排序的 Top 股票）",
            "stocks": rows,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_sentiment(signals: dict, breadth: float) -> str:
        """为情绪分析师提取数据。"""
        sent_data = signals.get("sentiment", {}).get("data", {})
        market = sent_data.get("market", {}) if sent_data else {}

        return json.dumps({
            "title": "情绪面数据",
            "市场广度": f"{int(breadth * 100)}%板块上涨",
            "情绪指数": market.get("sentiment_index", "?"),
            "炸板率": f"{market.get('po_ban_rate', 0) * 100:.0f}%" if market.get("po_ban_rate") else "?",
            "轮动速度": market.get("rotation_speed", "?"),
            "连板率": market.get("lianban_rate", "?"),
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_news(signals: dict) -> str:
        """为消息分析师提取数据。"""
        news_data = signals.get("news", {}).get("data", {})
        events = news_data.get("events", [])[:10] if news_data else []

        return json.dumps({
            "title": "消息面数据",
            "events": [{
                "标题": e.get("title", "")[:40],
                "情绪": e.get("sentiment", ""),
                "影响": e.get("impact", 0),
                "板块": e.get("sectors", []),
                "时间": e.get("time", ""),
            } for e in events],
            "总事件数": len(events),
        }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# 辩论编排器
# ═══════════════════════════════════════════════════════════════

class DebateOrchestrator:
    """Multi-Agent 辩论编排器。

    使用方式:
        do = DebateOrchestrator()
        report = do.run_debate(candidates, signals, hot_sectors, breadth)
        # report 包含三个分析师意见 + Moderator 综合判断
    """

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
            self._api_available = False
            return False

        import os
        from pathlib import Path
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            kf = Path(__file__).parent / "data" / "deepseek_key.txt"
            if kf.exists():
                key = kf.read_text().strip()
        if not key:
            self._api_available = False
            return False

        # P1-3: 显式 timeout，避免多次串行 LLM 调用时端点长时间挂起
        self._client = OpenAI(api_key=key, base_url="https://api.deepseek.com",
                              timeout=30.0)
        self._api_available = True
        return True

    def run_debate(
        self,
        candidates: list[dict],
        signals: dict,
        hot_sectors: list[str],
        breadth: float = 0.5,
        rounds: int = 1,
    ) -> Optional[dict]:
        """运行完整辩论流程。

        Args:
            rounds: 反驳轮数。0 = 退化为旧的「并行发言→聚合」（向后兼容）；
                    ≥1 = 真辩论：分析师看到彼此观点后互相反驳/补充/让步，再由主席收敛。

        Returns:
            {
                "analysts": {"tech": {...}, "sentiment": {...}, "news": {...}},
                "rebuttals": {"tech": {...}, ...}   # rounds≥1 时才有
                "moderator": {...},
                "consensus_level": str,
                "rounds": int,
                "timestamp": str,
            }
        """
        if not self._init_client():
            return None

        # Step 1: 三位分析师各自独立发表初始意见（只看自己领域数据）
        tech_data = DataExtractor.for_tech(candidates)
        sent_data = DataExtractor.for_sentiment(signals, breadth)
        news_data = DataExtractor.for_news(signals)

        tech_opinion = self._ask_analyst("tech", TECH_SYSTEM_PROMPT, tech_data)
        sent_opinion = self._ask_analyst("sentiment", SENTIMENT_SYSTEM_PROMPT, sent_data)
        news_opinion = self._ask_analyst("news", NEWS_SYSTEM_PROMPT, news_data)

        if not tech_opinion and not sent_opinion and not news_opinion:
            return None

        # Step 2: 反驳轮（真辩论核心）——每位分析师看到另外两位观点后回应
        rebuttals = {}
        if rounds >= 1:
            opinions = {"tech": tech_opinion, "sentiment": sent_opinion, "news": news_opinion}
            metas = {
                "tech": ("观象", "技术+资金面"),
                "sentiment": ("观势", "情绪面"),
                "news": ("观闻", "消息面"),
            }
            for role, (name, domain) in metas.items():
                if not opinions[role]:
                    continue
                others = {metas[r][0]: op for r, op in opinions.items() if r != role and op}
                reb = self._rebut(name, domain, opinions[role], others)
                if reb:
                    rebuttals[role] = reb

        # Step 3: Moderator 基于「初始意见 + 辩论反驳」综合收敛
        moderator_opinion = self._moderate(
            tech_opinion, sent_opinion, news_opinion,
            DataExtractor.for_tech(candidates, top_n=15),
            hot_sectors,
            rebuttals=rebuttals,
        )

        return {
            "analysts": {
                "tech": tech_opinion or {"error": "分析师不可用"},
                "sentiment": sent_opinion or {"error": "分析师不可用"},
                "news": news_opinion or {"error": "分析师不可用"},
            },
            "rebuttals": rebuttals,
            "moderator": moderator_opinion or {"error": "主席不可用"},
            "consensus_level": (moderator_opinion or {}).get("consensus_level", "未知"),
            "rounds": rounds,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _rebut(self, name: str, domain: str, own: dict, others: dict) -> Optional[dict]:
        """反驳轮：分析师看到另外两位观点后做出专业回应。"""
        own_str = json.dumps(own, ensure_ascii=False, indent=2)
        others_str = "\n\n".join(
            f"=== {n} 的观点 ===\n{json.dumps(op, ensure_ascii=False, indent=2)}"
            for n, op in others.items()
        )
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": REBUT_SYSTEM_PROMPT.format(analyst=name, domain=domain)},
                    {"role": "user", "content": (
                        f"你的初始意见：\n{own_str}\n\n"
                        f"另外两位分析师的观点：\n{others_str}\n\n"
                        "请做出你的辩论回应，严格按 JSON 输出。"
                    )},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=1500,
            )
            return self._parse_json(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("反驳轮 %s 调用失败: %s", name, e)
            return None

    def _ask_analyst(self, role: str, system_prompt: str, data: str) -> Optional[dict]:
        """询问一位分析师。"""
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": (
                        f"请根据以下数据发表你的专业意见。\n\n{data}\n\n"
                        "严格按照 JSON 格式输出，不要添加其他内容。"
                    )},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=1500,
            )
            raw = resp.choices[0].message.content
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("分析师 %s 调用失败: %s", role, e)
            return None

    def _moderate(
        self,
        tech: Optional[dict],
        sentiment: Optional[dict],
        news: Optional[dict],
        full_data: str,
        hot_sectors: list[str],
        rebuttals: dict = None,
    ) -> Optional[dict]:
        """Moderator 综合三方意见（含辩论反驳）。"""
        tech_str = json.dumps(tech, ensure_ascii=False, indent=2) if tech else "无数据"
        sent_str = json.dumps(sentiment, ensure_ascii=False, indent=2) if sentiment else "无数据"
        news_str = json.dumps(news, ensure_ascii=False, indent=2) if news else "无数据"

        context = (
            f"=== 技术分析师（观象）初始意见 === \n{tech_str}\n\n"
            f"=== 情绪分析师（观势）初始意见 === \n{sent_str}\n\n"
            f"=== 消息分析师（观闻）初始意见 === \n{news_str}\n\n"
        )
        # 把辩论反驳轮纳入主席视野——这是真辩论区别于并行聚合的关键
        if rebuttals:
            reb_str = json.dumps(rebuttals, ensure_ascii=False, indent=2)
            context += (
                f"=== 辩论反驳轮（分析师看到彼此观点后的回应）=== \n{reb_str}\n\n"
                "注意：请重点参考辩论环节中的反驳、让步与修正后观点——"
                "被对方说服的让步、无人反驳的共识，比初始意见更可信。\n\n"
            )
        context += (
            f"=== 完整候选池 === \n{full_data}\n\n"
            f"=== 当前热板块 === \n{hot_sectors[:8]}"
        )

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": MODERATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"请综合三位分析师的意见、辩论反驳轮和完整候选池数据，给出最终决策建议。\n\n{context}\n\n"
                        "严格按照 JSON 格式输出。"
                    )},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=2500,
            )
            raw = resp.choices[0].message.content
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("Moderator 调用失败: %s", e)
            return None

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        try:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            return json.loads(m.group() if m else raw)
        except (json.JSONDecodeError, AttributeError):
            return None


# ── 全局单例 ──────────────────────────────────────────────────

_orchestrator: Optional[DebateOrchestrator] = None


def get_orchestrator() -> DebateOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DebateOrchestrator()
    return _orchestrator
