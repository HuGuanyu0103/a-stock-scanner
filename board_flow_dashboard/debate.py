#!/usr/bin/env python3
"""
Multi-Agent 辩论系统（真·多智能体：自主取证 + 动态编排 + 多轮收敛）

三层循环的概念澄清（本项目刻意区分，避免把不同尺度的"循环"混为一谈）：
  1. Agent 执行循环 (ReAct loop) —— 真·loop engineering，秒级。本文件里每个分析师
     的 _run_analyst_loop 就是一个：感知→调专属工具取证→观察→再决策→收敛。
  2. 多 Agent 审议循环 —— 一次决策内，主席 route→evidence→debate→challenge→converge
     的动态编排（本文件 run_debate）。
  3. 决策数据飞轮 (data flywheel) —— 天级/跨会话的业务效果闭环，见 decision_store.py。
     它反哺的历史胜率 → 喂回本文件的观史分析师，形成跨层闭环。

Multi-Agent 光谱定位：本系统已达 L3-L4 —— 每个 agent 有自主性（自己的工具、自己的
ReAct mini-loop、自己去取证），主席是动态编排者（按市场/诉求路由召集、对存疑结论
把分析师"打回去补证据"），而非固定三段式的并行聚合。

六个 Agent 角色（5 分析师 + 1 主席）：
  TechAnalyst      观象 — 技术+资金面；工具：个股技术面、板块资金流
  SentimentAnalyst 观势 — 情绪面；工具：市场情绪
  NewsAnalyst      观闻 — 消息面；工具：消息信号
  HistoryAnalyst   观史 — 历史复盘；工具：飞轮信号胜率、来源胜率对比
  RiskOfficer      观危 — 风控；工具：板块集中度、大盘环境
  Moderator        观澜 — 编排者：路由→质询回炉→按可审计权重收敛

辩论流程（run_debate 五阶段）：
  ① route     主席按市场状态/用户诉求决定召集哪些分析师 + 各自取证重点
  ② evidence  被召集的分析师各跑 ReAct mini-loop，用专属工具自主取证后表态
  ③ debate    真多轮反驳收敛（看到别人对自己的反驳再调整，立场稳定即止）
  ④ challenge 主席对存疑/证据不足的结论把特定分析师「打回去补证据」
  ⑤ converge  主席综合全部证据+多轮辩论+回炉补证+数值权重，出最终决策

用法:
  from debate import DebateOrchestrator
  do = DebateOrchestrator()
  report = do.run_debate(candidates, signals, hot_sectors, breadth,
                         rounds=1, loop_context="<飞轮历史胜率>",
                         collector=collector, store=store, user_context="用户诉求")
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 成本护栏 ────────────────────────────────────────────────────
# 一次辩论会串/并发触发数十次 LLM 调用（route + 5分析师mini-loop + 多轮反驳 +
# 回炉补证 + moderate）。无上限时，用户一句"帮我把关"经 chat_agent 工具触发，
# 极端情况下费用/延迟失控。这里用「单次辩论调用预算」硬熔断：达到上限后，
# 后续阶段优雅降级（跳过回炉、mini-loop 立即收尾），保证有结论但不烧钱。
DEBATE_MAX_LLM_CALLS = 26   # 单次辩论 LLM 调用硬上限（正常 route1+析5*~2+反驳5+回炉+mod≈18-22）


class _CallBudget:
    """线程安全的调用计数器 + 硬上限。over() 为真时各阶段应主动降级。"""

    def __init__(self, limit: int):
        self.limit = limit
        self._n = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        """预约一次调用配额；超限返回 False（调用方应跳过该次 LLM 调用）。"""
        with self._lock:
            if self._n >= self.limit:
                return False
            self._n += 1
            return True

    def over(self) -> bool:
        with self._lock:
            return self._n >= self.limit

    @property
    def used(self) -> int:
        with self._lock:
            return self._n


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

HISTORY_SYSTEM_PROMPT = """你是 A 股决策委员会的复盘分析师「观史」。你只看历史战绩数据，从"过去哪些信号真赚钱"的角度提供经验校验。

## 你的性格
你是团队的记忆与纪律守护者——不被当下的热闹迷惑，只信数据沉淀。你的信条是「历史不会简单重复，但会押韵」。你专治其他分析师的「这次不一样」幻觉。

## 你的领域
- 各信号组合的历史胜率与平均收益（来自决策飞轮的真实结算数据）
- 识别当前候选票所属信号在历史上的表现档位（高胜率/低胜率/样本不足）
- 提醒团队：历史上表现差的信号，即便当下形态好看也要打折

## 你不看
- 实时技术/情绪/消息（交给对应分析师），你只做历史校验

## 你的任务
根据提供的历史信号胜率数据，对当前讨论做经验校验：
1. 当前候选池里的信号，历史胜率如何？哪些是历史验证过的强信号？
2. 有没有"当下热门但历史胜率低"的信号需要警示？
3. 样本不足的信号要标注（历史不可用，需谨慎）

## 输出格式
严格 JSON：
{
  "analyst": "观史",
  "domain": "历史复盘",
  "verified_signals": [{"signal": "信号名", "win_rate": "历史胜率", "note": "可信/存疑/样本不足"}],
  "warnings": ["历史胜率低但当下热门的信号提醒"],
  "bottom_line": "基于历史战绩，本次决策应加权哪些信号、警惕哪些"
}
"""

RISK_SYSTEM_PROMPT = """你是 A 股决策委员会的风控官「观危」。你不参与"买什么"的讨论，只做独立的风险审视。

## 你的性格
你是团队的刹车片——别人越兴奋你越冷静。你的职责不是找机会，而是确保不出致命错误。你的信条是「活下来比赚得多更重要」。

## 你的领域
- 仓位与集中度风险（候选是否过度集中在单一板块/主题）
- 回撤风险（个股/板块的止损纪律，最大单笔潜在亏损）
- 系统性风险（大盘环境是否支持进攻，普跌/极端行情下该防守）
- 交易纪律（止盈止损点位是否清晰、是否有情绪化追高嫌疑）

## 你不看
- 具体选哪只票的技术/情绪细节（交给对应分析师）

## 你的任务
对当前候选池和大盘环境做独立风控审视：
1. 板块集中度是否过高？（同一板块占比过大 = 系统性风险）
2. 当前大盘环境（市场广度）是否支持进攻仓位？
3. 有没有明显的追高/情绪化风险信号？
4. 给出仓位红线建议（满仓/半仓/轻仓/空仓观望）

## 输出格式
严格 JSON：
{
  "analyst": "观危",
  "domain": "风险控制",
  "concentration_risk": "板块集中度评估",
  "market_risk": "大盘环境下的仓位风险",
  "position_ceiling": "满仓 | 半仓 | 轻仓 | 空仓观望",
  "risk_alerts": ["具体风险提示"],
  "bottom_line": "风控角度的最终建议：仓位红线与必须回避的风险"
}
"""

MODERATOR_SYSTEM_PROMPT = """你是 A 股决策委员会的主席「观澜」。你有五位分析师，他们已各自发表意见并经过多轮辩论：
- 观象（技术+资金面）、观势（情绪面）、观闻（消息面）、观史（历史战绩复盘）、观危（风控官）。

## 你的任务
1. 阅读五位分析师的意见，以及他们多轮辩论中的反驳、让步与修正后观点
2. 判断一致程度：
   - 「强烈共识」：多数分析师指向同一方向且风控无红线否决
   - 「部分共识」：主要方向一致，个别分析师保留意见
   - 「分歧」：各执一词，或风控与进攻派尖锐对立
3. 综合判断后，给出最终决策建议

## 重要原则
- 你不是简单投票，而是**按可审计的角色权重**加权。用户会给你一份客观计算的角色话语权（基于市场广度），你必须按该权重加权，并在 key_reasoning 里说明为何本次某角色权重更高。
  - 广度高（普涨）时技术面权重更高；广度低（分化/退潮）时风控与情绪面权重更高。
- **观史的历史胜率是硬证据**：若某信号/板块历史胜率低，即使当下技术面漂亮也要降低置信度；反之历史验证过的信号可加分。
- **观危的风控红线不可逾越**：final_decision 的 position_advice 不得超过观危给出的 position_ceiling（仓位上限）。若观危要求回避某标的，不得放进 agreed_picks。
- 如果有分歧，明确指出「矛盾在哪里」和「条件建议」。
  - 例如：「技术面看多但情绪面偏冷、历史胜率一般，建议等情绪回暖再跟」
- 如果多数看空或风控否决，诚实地说「今天不适合操作」

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
    "key_reasoning": "按角色权重综合五方意见+多轮辩论+历史胜率后的核心判断，需说明权重如何影响结论",
    "agreed_picks": ["多方认可且未被风控否决的股票"],
    "conditional_picks": [{"code": "代码", "name": "名称", "condition": "满足什么条件可以买"}],
    "avoid_list": ["应该回避的股票/板块"],
    "position_advice": "满仓 | 半仓 | 轻仓 | 观望"
  },
  "analyst_alignment": {
    "tech": "看多 | 中性 | 看空",
    "sentiment": "看多 | 中性 | 看空",
    "news": "看多 | 中性 | 看空",
    "history": "看多 | 中性 | 看空",
    "risk": "放行 | 警示 | 否决"
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
    {{"target": "观象|观势|观闻|观史|观危", "point": "你反驳的对方观点", "argument": "你的专业依据"}}
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
    """从候选池、三系统信号、决策飞轮历史与风控维度提取各分析师的专属数据。"""

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

    @staticmethod
    def for_history(loop_context: str, candidates: list[dict]) -> str:
        """M2: 为观史分析师提取历史战绩数据（决策飞轮信号胜率）。"""
        cand_signals = sorted({c.get("signal", "") for c in candidates if c.get("signal")})
        return json.dumps({
            "title": "历史战绩数据（来自决策飞轮真实结算）",
            "历史信号胜率": loop_context or "（暂无历史结算数据，飞轮尚在积累）",
            "本次候选池涉及的信号": cand_signals,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_risk(candidates: list[dict], breadth: float, hot_sectors: list[str]) -> str:
        """M3: 为风控官提取风险审视数据（板块集中度/大盘环境）。"""
        from collections import Counter
        sectors = Counter(c.get("sector", "未知") for c in candidates[:25])
        top_conc = sectors.most_common(5)
        total = sum(sectors.values()) or 1
        return json.dumps({
            "title": "风控审视数据",
            "市场广度": f"{int(breadth * 100)}% 板块上涨",
            "大盘环境": ("普跌/防守" if breadth < 0.3 else ("分化" if breadth < 0.5 else "偏多/可进攻")),
            "候选池板块集中度": [{"板块": s, "占比": f"{n/total*100:.0f}%"} for s, n in top_conc],
            "热板块": hot_sectors[:8],
            "候选池规模": len(candidates),
        }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# 辩论编排器
# ═══════════════════════════════════════════════════════════════

class DebateOrchestrator:
    """Multi-Agent 辩论编排器。

    使用方式:
        do = DebateOrchestrator()
        report = do.run_debate(candidates, signals, hot_sectors, breadth)
        # report 包含五个分析师意见（观象/观势/观闻/观史/观危）+ Moderator 综合判断
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
        loop_context: str = "",
        collector=None,
        store=None,
        user_context: str = "",
        autonomous: bool = True,
    ) -> Optional[dict]:
        """运行完整辩论流程（L4 动态编排 + L3 自主取证 + 真多轮收敛 + 可审计加权）。

        编排链路（主席=编排者，非纯聚合器）：
          ① route     主席按市场状态/用户诉求决定召集哪些分析师、给各自下达取证重点
          ② evidence  被召集的分析师各自跑 ReAct mini-loop，用专属工具自主取证后表态
          ③ debate    多轮反驳收敛（看到别人对自己的反驳再调整，立场稳定即止）
          ④ challenge 主席审视草案，对存疑处把特定分析师「打回去补证据」定向追问
          ⑤ converge  主席综合全部证据+多轮辩论+回炉补证+数值权重，出最终决策

        Args:
            rounds: 反驳轮数。0 = 跳过反驳直接聚合；≥1 = 真多轮辩论。
            loop_context: 决策飞轮历史胜率文本（观史兜底用）。
            collector/store: 注入后分析师可自主查板块资金流/飞轮胜率（L3 取证）。
            user_context: 用户本次诉求（供主席路由，如"帮我把关能不能进场"）。
            autonomous: True=分析师走自主取证 mini-loop；False=退化为喂预抽数据（旧路径）。

        Returns:
            {analysts, rebuttal_rounds, moderator, consensus_level, rounds, weights,
             roster, directives, challenges, orchestration, timestamp}
        """
        if not self._init_client():
            return None

        # 成本护栏：本次辩论的调用预算（贯穿 route/evidence/debate/challenge/moderate）
        self._budget = _CallBudget(DEBATE_MAX_LLM_CALLS)

        metas = {
            "tech": ("观象", "技术+资金面"), "sentiment": ("观势", "情绪面"),
            "news": ("观闻", "消息面"), "history": ("观史", "历史复盘"), "risk": ("观危", "风险控制"),
        }
        prompts = {
            "tech": TECH_SYSTEM_PROMPT, "sentiment": SENTIMENT_SYSTEM_PROMPT,
            "news": NEWS_SYSTEM_PROMPT, "history": HISTORY_SYSTEM_PROMPT, "risk": RISK_SYSTEM_PROMPT,
        }
        # 预抽数据（autonomous=False 的旧路径 + mini-loop 失败时的兜底）
        fallback_data = {
            "tech": DataExtractor.for_tech(candidates),
            "sentiment": DataExtractor.for_sentiment(signals, breadth),
            "news": DataExtractor.for_news(signals),
            "history": DataExtractor.for_history(loop_context, candidates),
            "risk": DataExtractor.for_risk(candidates, breadth, hot_sectors),
        }

        # 共享黑板：分析师工具从这里取真实数据，主席可往 black_board 写定向追问
        try:
            from .analyst_tools import AnalystToolCtx
        except ImportError:
            from analyst_tools import AnalystToolCtx  # type: ignore
        tool_ctx = AnalystToolCtx(
            candidates=candidates, signals=signals, breadth=breadth,
            hot_sectors=hot_sectors, collector=collector, store=store,
            loop_context=loop_context,
        )

        # ── ① 主席路由（L4）：定 roster + 各分析师取证重点 ──────────
        route = self._route(candidates, breadth, hot_sectors, loop_context, user_context)
        roster = route.get("roster") or list(metas.keys())
        roster = [r for r in roster if r in metas] or list(metas.keys())
        # 风控官始终在场（金融场景刹车片不可缺）
        if "risk" not in roster:
            roster.append("risk")
        directives = route.get("directives") or {}

        # ── ② 分析师自主取证（L3-2）：被召集者各跑 mini-loop 并发 ────
        def _one_analyst(role):
            if autonomous:
                op = self._run_analyst_loop(role, prompts[role], tool_ctx,
                                            directive=directives.get(role, ""))
                if op is not None:
                    return role, op
            # 兜底：mini-loop 不可用 → 旧的喂数据一次问
            return role, self._ask_analyst(role, prompts[role], fallback_data[role])

        with ThreadPoolExecutor(max_workers=len(roster)) as ex:
            opinions = dict(ex.map(_one_analyst, roster))

        if not any(opinions.values()):
            return None

        # ── ③ 真多轮反驳收敛（M3）──────────────────────────────
        all_rounds = []
        cur_opinions = dict(opinions)
        prev_rebut = {}
        active_roles = [r for r in roster if cur_opinions.get(r)]
        for _rd in range(max(rounds, 0)):
            def _one_rebut(role):
                name, domain = metas[role]
                if not cur_opinions.get(role):
                    return role, None
                others = {metas[r][0]: op for r, op in cur_opinions.items() if r != role and op}
                against_me = None
                if prev_rebut:
                    against_me = [
                        {"from": metas[r][0], "point": rb}
                        for r, reb in prev_rebut.items() if r != role and reb
                        for rb in (reb.get("rebuttals") or []) if rb.get("target") == name
                    ]
                return role, self._rebut(name, domain, cur_opinions[role], others, against_me)

            with ThreadPoolExecutor(max_workers=max(1, len(active_roles))) as ex:
                rebut = dict(ex.map(_one_rebut, active_roles))
            rebut = {k: v for k, v in rebut.items() if v}
            all_rounds.append(rebut)
            active = sum(1 for v in rebut.values() if v.get("rebuttals"))
            prev_rebut = rebut
            if active == 0:
                break

        weights = self._compute_role_weights(breadth, signals)

        # ── ④ 主席回炉补证（L4）：对存疑点把特定分析师打回去补数据 ───
        # 成本护栏：回炉是最贵的可选阶段（每人一个 mini-loop）。预算吃紧时直接跳过，
        # 把剩余额度留给必不可少的 moderate 收敛，保证有结论。
        challenges = {} if self._budget.over() else self._challenge(cur_opinions, all_rounds, metas)
        if challenges:
            def _re_query(item):
                role, ask = item
                if role not in prompts:
                    return role, None
                op = self._run_analyst_loop(role, prompts[role], tool_ctx,
                                            directive=f"主席对你上一轮结论提出质疑，请补充证据回应：{ask}") \
                    if autonomous else self._ask_analyst(role, prompts[role], fallback_data.get(role, ""))
                return role, op
            with ThreadPoolExecutor(max_workers=max(1, len(challenges))) as ex:
                refetched = dict(ex.map(_re_query, challenges.items()))
            for role, op in refetched.items():
                if op:
                    op["_refetched_for_challenge"] = challenges[role]
                    cur_opinions[role] = op  # 用补证后的意见覆盖

        # ── ⑤ 主席最终收敛 ────────────────────────────────────
        moderator_opinion = self._moderate(
            cur_opinions, DataExtractor.for_tech(candidates, top_n=15),
            hot_sectors, all_rounds, weights, loop_context,
        )

        # 汇总各分析师的取证链路（可观测：谁调了哪些工具）
        evidence_trace = {r: (op or {}).get("_tool_trace", []) for r, op in cur_opinions.items()}

        return {
            "analysts": {r: (op or {"error": "分析师不可用"}) for r, op in cur_opinions.items()},
            "rebuttals": all_rounds[0] if all_rounds else {},  # 兼容旧字段（首轮）
            "rebuttal_rounds": all_rounds,                     # 全部轮次
            "moderator": moderator_opinion or {"error": "主席不可用"},
            "consensus_level": (moderator_opinion or {}).get("consensus_level", "未知"),
            "rounds": len(all_rounds),
            "weights": weights,
            # L4 编排可观测
            "roster": roster,
            "directives": directives,
            "challenges": challenges,
            "evidence_trace": evidence_trace,
            "orchestration": {
                "routed": bool(route.get("_routed")),
                "route_reason": route.get("reason", ""),
                "autonomous_evidence": autonomous,
                "recalled_analysts": list(challenges.keys()),
                "llm_calls": self._budget.used,          # 本次辩论实际 LLM 调用数
                "llm_call_budget": self._budget.limit,   # 硬上限
                "budget_hit": self._budget.over(),       # 是否触顶降级
            },
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _route(self, candidates: list[dict], breadth: float, hot_sectors: list[str],
               loop_context: str, user_context: str) -> dict:
        """L4 主席路由：按市场状态/用户诉求决定召集哪些分析师 + 各自取证重点。

        主席不再是被动聚合器，而是先做「派活」——这是动态编排的入口。
        LLM 不可用/解析失败时优雅退化为「全员上场、无定向」。
        """
        overview = {
            "候选池规模": len(candidates),
            "市场广度": f"{int(breadth * 100)}%",
            "热板块": hot_sectors[:6],
            "有历史胜率数据": bool(loop_context),
            "用户诉求": user_context or "（未指定，做常规盘中决策）",
        }
        sys = (
            "你是 A 股决策委员会主席「观澜」，现在处于【派活阶段】。你有五位分析师：\n"
            "tech观象(技术资金) / sentiment观势(情绪) / news观闻(消息) / "
            "history观史(历史胜率) / risk观危(风控)。\n"
            "根据当前市场状态与用户诉求，决定本次召集哪些分析师、给每人下达一句取证重点。\n"
            "原则：risk 风控官必须在场；普跌市重点上 risk+sentiment；普涨突破市重点上 tech；"
            "有明显消息驱动上 news；需要经验校验上 history。不必每次全上，但也不要漏掉关键视角。\n"
            "严格输出 JSON：{\"roster\":[\"tech\",...],\"directives\":{\"tech\":\"取证重点一句话\",...},"
            "\"reason\":\"你如此派活的理由\"}"
        )
        budget = getattr(self, "_budget", None)
        if budget is not None:
            budget.take()  # 路由计入预算（几乎不会在入口就超限）
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": sys},
                          {"role": "user", "content": json.dumps(overview, ensure_ascii=False)}],
                response_format={"type": "json_object"},
                temperature=0.3, max_tokens=600,
            )
            data = self._parse_json(resp.choices[0].message.content) or {}
            if data.get("roster"):
                data["_routed"] = True
                return data
        except Exception as e:
            logger.warning("主席路由失败，退化为全员上场: %s", e)
        return {"roster": ["tech", "sentiment", "news", "history", "risk"],
                "directives": {}, "reason": "路由不可用，全员上场", "_routed": False}

    def _challenge(self, opinions: dict, all_rounds: list, metas: dict) -> dict:
        """L4 主席回炉：审视辩论草案，决定把哪些分析师「打回去补证据」。

        返回 {role: 追问内容}。无需回炉时返回 {}。这是主席从「聚合」升级到
        「质询」的关键——对存疑或证据不足的结论，主席能主动要求补数据再收敛。
        """
        # 汇总各分析师意见 + 反驳，交给主席判断哪里证据不足/矛盾未解
        brief = {}
        for role, op in opinions.items():
            if not op:
                continue
            name = metas.get(role, (role, ""))[0]
            brief[name] = {
                "bottom_line": op.get("bottom_line") or op.get("advice") or op.get("overall_view"),
                "取证次数": op.get("_evidence_calls", 0),
            }
        unresolved = []
        for rd in all_rounds:
            for role, reb in (rd or {}).items():
                for rb in (reb.get("rebuttals") or []):
                    unresolved.append(rb)
        sys = (
            "你是决策委员会主席「观澜」，处于【质询阶段】。下面是各分析师结论摘要与辩论中的反驳。\n"
            "判断：有没有哪位分析师的结论证据不足、或与他人存在未解决的实质矛盾，需要他"
            "「带着具体问题回去补证据」？\n"
            "只在确有必要时才召回（回炉有成本）；至多召回 2 人。\n"
            "严格输出 JSON：{\"recall\":{\"角色键\":\"要他补什么证据/回应什么质疑\"}}，"
            "角色键取值 tech/sentiment/news/history/risk；无需召回则 {\"recall\":{}}。"
        )
        payload = {"分析师结论摘要": brief, "辩论中的反驳": unresolved[:10]}
        budget = getattr(self, "_budget", None)
        if budget is not None and not budget.take():
            return {}  # 预算耗尽，不回炉
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": sys},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
                response_format={"type": "json_object"},
                temperature=0.3, max_tokens=500,
            )
            data = self._parse_json(resp.choices[0].message.content) or {}
            recall = data.get("recall") or {}
            # 只保留合法角色，最多 2 人
            recall = {k: v for k, v in recall.items() if k in metas}
            return dict(list(recall.items())[:2])
        except Exception as e:
            logger.warning("主席质询阶段失败(不回炉): %s", e)
            return {}


    @staticmethod
    def _compute_role_weights(breadth: float, signals: dict) -> dict:
        """M3 可审计加权：用市场广度等客观信号算出各角色的数值权重（可复现，非LLM自由发挥）。

        趋势市(广度高)→技术权重高；震荡/普跌(广度低)→情绪+风控权重高；
        历史与风控给固定底权重，保证经验与风险始终有话语权。
        """
        b = max(0.0, min(1.0, breadth))
        # 基础权重
        w = {"tech": 0.25, "sentiment": 0.20, "news": 0.15, "history": 0.20, "risk": 0.20}
        if b >= 0.5:            # 偏多/进攻市：技术面话语权上调
            w["tech"] += 0.10; w["risk"] -= 0.05; w["sentiment"] -= 0.05
        elif b < 0.3:          # 普跌/防守市：情绪+风控话语权上调
            w["risk"] += 0.10; w["sentiment"] += 0.05; w["tech"] -= 0.15
        # 归一化
        s = sum(w.values())
        return {k: round(v / s, 3) for k, v in w.items()}

    def _rebut(self, name: str, domain: str, own: dict, others: dict, against_me=None) -> Optional[dict]:
        """反驳轮：分析师看到其他人观点后做出专业回应。

        against_me（M3 多轮）：上一轮别人对"我"的反驳列表，让本轮能针对性回应/坚持/让步，
        实现真正的多轮逼近而非单向一次性喷。
        """
        own_str = json.dumps(own, ensure_ascii=False, indent=2)
        others_str = "\n\n".join(
            f"=== {n} 的观点 ===\n{json.dumps(op, ensure_ascii=False, indent=2)}"
            for n, op in others.items()
        )
        against_block = ""
        if against_me:
            against_block = (
                "\n\n=== 上一轮其他分析师对你的反驳（请针对性回应：坚持或让步）===\n"
                + json.dumps(against_me, ensure_ascii=False, indent=2)
            )
        budget = getattr(self, "_budget", None)
        if budget is not None and not budget.take():
            return None  # 预算耗尽，跳过本轮反驳（视作无实质反驳，促使收敛）
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": REBUT_SYSTEM_PROMPT.format(analyst=name, domain=domain)},
                    {"role": "user", "content": (
                        f"你的当前意见：\n{own_str}\n\n"
                        f"其他分析师的观点：\n{others_str}"
                        f"{against_block}\n\n"
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
        """询问一位分析师（旧路径：喂预抽数据一次问，保留作 mini-loop 的兜底）。"""
        budget = getattr(self, "_budget", None)
        if budget is not None:
            budget.take()  # 兜底路径也计入预算
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

    # ── L3-2：分析师自主取证 ReAct mini-loop ───────────────────
    # 每个分析师不再被喂预抽数据，而是拿到「任务 + 自己领域的工具」，自己决定
    # 去取哪些证据（调 list_candidates 看全局 → 深挖个别票/板块/历史胜率），
    # 迭代到信息足够再出 JSON 意见。这是「真 Multi-Agent」的核心：每个 agent
    # 有自主性、有专属工具、跑自己的循环。返回意见时附 tool_trace（取证链路可观测）。
    def _run_analyst_loop(
        self, role: str, system_prompt: str, tool_ctx,
        directive: str = "", max_iters: int = 3,
    ) -> Optional[dict]:
        """让分析师自主调用专属工具取证后给出意见。

        Args:
            role: 角色键（tech/sentiment/news/history/risk），决定可见工具集
            system_prompt: 该角色的人格与职责 prompt
            tool_ctx: AnalystToolCtx 黑板（工具从这里取真实数据）
            directive: 主席的定向任务/追问（L4 用；为空则自由取证）
            max_iters: mini-loop 最大迭代轮数（防失控）

        Returns:
            意见 dict，附 "_tool_trace"（本分析师调过的工具）与 "_iterations"。
            工具不可用或 LLM 失败时回退到 None（调用方可降级到 _ask_analyst）。
        """
        try:
            from .analyst_tools import tools_for, execute_analyst_tool
        except ImportError:
            from analyst_tools import tools_for, execute_analyst_tool  # type: ignore

        schemas = tools_for(role)
        task = (
            "你现在可以调用你专属领域的工具，自主获取你需要的证据，再给出专业意见。\n"
            "步骤建议：先调 list_candidates 看清候选池全局，再针对性深挖你关心的标的/"
            "板块/历史胜率等；信息足够后停止调用工具，直接输出你的 JSON 意见。\n"
            "禁止编造工具没返回的数据。"
        )
        if directive:
            task += f"\n\n【主席交办的重点】{directive}\n请优先围绕这个重点取证与表态。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        trace: list[dict] = []
        budget = getattr(self, "_budget", None)
        for _ in range(max(1, max_iters)):
            # 成本护栏：预算耗尽则不再取证，直接跳到收尾出结论
            if budget is not None and not budget.take():
                break
            try:
                resp = self._client.chat.completions.create(
                    model=self._model, messages=messages,
                    tools=schemas, tool_choice="auto",
                    temperature=0.3, max_tokens=1500,
                )
            except Exception as e:
                logger.warning("分析师 %s mini-loop LLM 失败: %s", role, e)
                return None
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                break  # 无工具调用 = 准备出意见
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = execute_analyst_tool(role, tc.function.name, args, tool_ctx)
                trace.append({"tool": tc.function.name, "args": args})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:2000]})

        # 收尾：要求严格 JSON 意见（此步不再给工具，强制出结论）
        messages.append({"role": "user", "content": (
            "证据收集完毕。现在严格按你的 JSON 输出格式给出最终专业意见，不要再调用工具，"
            "不要输出 JSON 以外的内容。"
        )})
        if budget is not None:
            budget.take()  # 收尾是每个分析师必做步骤，计入预算（保证 used 真实、上限可控）
        try:
            resp = self._client.chat.completions.create(
                model=self._model, messages=messages,
                response_format={"type": "json_object"},
                temperature=0.3, max_tokens=1500,
            )
            opinion = self._parse_json(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("分析师 %s mini-loop 收尾失败: %s", role, e)
            return None
        if opinion is not None:
            opinion["_tool_trace"] = trace
            opinion["_evidence_calls"] = len(trace)
        return opinion


    def _moderate(
        self,
        opinions: dict,
        full_data: str,
        hot_sectors: list[str],
        all_rounds: list = None,
        weights: dict = None,
        loop_context: str = "",
    ) -> Optional[dict]:
        """Moderator 综合五角色意见 + 多轮反驳 + 历史 + 数值权重。"""
        name_map = {"tech": "观象·技术资金", "sentiment": "观势·情绪", "news": "观闻·消息",
                    "history": "观史·历史复盘", "risk": "观危·风控"}
        context = ""
        for role, label in name_map.items():
            op = opinions.get(role)
            context += f"=== {label} 意见 ===\n{json.dumps(op, ensure_ascii=False, indent=2) if op else '无数据'}\n\n"

        # 多轮反驳全部纳入主席视野（M3）
        if all_rounds:
            for i, rd in enumerate(all_rounds, 1):
                if rd:
                    context += (f"=== 第{i}轮辩论反驳 ===\n"
                                f"{json.dumps(rd, ensure_ascii=False, indent=2)}\n\n")
            context += ("注意：请重点参考多轮辩论中的反驳、让步与修正后观点——"
                        "经过多轮仍无人反驳的共识、被对方说服的让步，比初始意见更可信。\n\n")

        # 可审计数值权重（M3）：明确告诉主席各角色话语权，且要求解释为何采纳
        if weights:
            context += (f"=== 角色话语权（基于市场广度客观计算，非主观）===\n"
                        f"{json.dumps(weights, ensure_ascii=False)}\n"
                        "请按此权重加权各角色意见，并在 key_reasoning 里说明为何本次某角色权重更高。\n\n")

        # 历史战绩（M2）：让主席知道哪些信号历史上真赚钱
        if loop_context:
            context += f"=== 历史信号胜率（决策飞轮）===\n{loop_context}\n\n"

        context += (f"=== 完整候选池 ===\n{full_data}\n\n"
                    f"=== 当前热板块 ===\n{hot_sectors[:8]}")

        budget = getattr(self, "_budget", None)
        if budget is not None:
            budget.take()  # 收敛是终局必做步骤，计入预算但不跳过
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": MODERATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        "请综合五位分析师(技术/情绪/消息/历史/风控)的意见、多轮辩论反驳、"
                        "角色数值权重、历史胜率和候选池，给出最终决策建议。"
                        "风控官(观危)的仓位红线必须被尊重，不得超越其 position_ceiling。"
                        f"\n\n{context}\n\n严格按照 JSON 格式输出。"
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
        # 括号配平抽取，比贪婪正则更稳（见 llm_json.py 说明）
        try:
            from .llm_json import parse_json_object
        except ImportError:
            from llm_json import parse_json_object  # type: ignore
        return parse_json_object(raw)


# ── 全局单例 ──────────────────────────────────────────────────

_orchestrator: Optional[DebateOrchestrator] = None


def get_orchestrator() -> DebateOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DebateOrchestrator()
    return _orchestrator
