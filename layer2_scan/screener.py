"""
选股引擎 — 结合行情过滤 + 技术形态扫描 + 资金流向 + 综合评分

工作流程:
  1. 获取全市场股票代码 → 分批获取行情 → 初筛
  2. 批量获取 K 线数据
  3. 对每只股票运行 TechnicalPatterns.scan()
  4. 综合评分并排序输出
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from layer1_data import DataFetcher
from layer2_scan.patterns import TechnicalPatterns
from layer2_scan.sentiment_signals import SentimentSignals
from layer2_scan.alpha_factors import AlphaFactors

logger = logging.getLogger(__name__)

DEFAULT_KLINE_DAYS = 120

# list_market_current() 实际返回的列名
# 注意：volume 字段实际是成交额（金额，单位万元），amount 也是成交额
_MARKET_COLS = ("stock_code", "short_name", "price", "change", "change_pct",
                "volume", "amount")


class StockScreener:
    """选股引擎"""

    def __init__(self, fetcher: Optional[DataFetcher] = None, max_workers: int = 8):
        self.fetcher = fetcher or DataFetcher(max_workers=max_workers)
        self.max_workers = max_workers

    def screen(
        self,
        min_price: float = 3.0,
        max_price: float = 100.0,
        min_volume: float = 0,           # 最低成交额(万)
        min_turnover: float = 0,         # 最低换手率 (若数据不可用则忽略)
        min_market_cap: float = 0,
        max_market_cap: float = 500,
        exclude_st: bool = True,
        exclude_beijing: bool = False,
        exclude_chinext: bool = True,
        top_n: int = 50,
        min_score: int = 0,
        batch_size: int = 50,
        enable_sentiment: bool = True,
        enable_factors: bool = True,            # 每批获取行情的股票数
    ) -> pd.DataFrame:
        """
        执行全市场选股扫描

        Returns
        -------
        DataFrame with columns:
            stock_code, short_name, price, change_pct, volume, amount,
            signal_score, max_signal_level, signal_count,
            sentiment_score, factor_score, combined_score,
            signal_names
        """
        logger.info("开始全市场选股扫描...")

        # 1. 获取所有股票代码
        all_stocks = self.fetcher.all_stocks()
        if all_stocks is None or all_stocks.empty:
            logger.warning("无法获取股票列表")
            return pd.DataFrame()
        all_codes = all_stocks["stock_code"].tolist()
        logger.info("共 %d 只股票", len(all_codes))

        # 2. 分批获取行情（adata 不支持全量一次请求）
        market_fragments = []
        for i in range(0, len(all_codes), batch_size):
            chunk = all_codes[i:i + batch_size]
            try:
                df = self.fetcher.current_market(code_list=chunk)
                if df is not None and not df.empty:
                    market_fragments.append(df)
            except Exception as e:
                logger.debug("批次 %d 行情获取失败: %s", i, e)
        if not market_fragments:
            logger.warning("无法获取行情数据")
            return pd.DataFrame()

        market_df = pd.concat(market_fragments, ignore_index=True)
        logger.info("行情获取完成: %d 只", len(market_df))

        # 3. 应用初筛条件
        market_df = self._filter_market(market_df, min_price, max_price, min_volume,
                                         min_turnover, min_market_cap, max_market_cap,
                                         exclude_st, exclude_beijing, exclude_chinext)
        if market_df.empty:
            logger.warning("初筛无结果")
            return pd.DataFrame()

        candidate_df = market_df.sort_values("amount", ascending=False)
        codes = candidate_df["stock_code"].tolist()
        logger.info("K线扫描候选: %d 只（全量）", len(codes))
        kline_map = self.fetcher.batch_kline(codes, days=DEFAULT_KLINE_DAYS)
        logger.info("K 线获取完成: %d/%d", len(kline_map), len(codes))

        # 5. 技术形态扫描
        results = []
        for code in codes:
            row = candidate_df[candidate_df["stock_code"] == code].iloc[0]
            kline_df = kline_map.get(code)
            if kline_df is None or kline_df.empty:
                continue

            # 技术形态信号
            tech_signals = TechnicalPatterns.scan(kline_df)
            tech_score = TechnicalPatterns.total_score(tech_signals)
            if tech_score < min_score and not enable_factors:
                continue

            # 情绪面信号
            sent_score = 0.0
            sent_signals = []
            if enable_sentiment:
                sent_signals = SentimentSignals.scan(kline_df)
                sent_score = SentimentSignals.total_score(sent_signals)

            # 因子评分
            factor_score = 0.0
            factor_details = {}
            if enable_factors:
                try:
                    af = AlphaFactors(kline_df)
                    factor_details = af.compute_all()
                    factor_score = af.composite_score()
                except Exception as e:
                    logger.debug("因子计算失败 %s: %s", code, e)

            # 合并信号
            all_signals = tech_signals + sent_signals
            total_tech_score = tech_score + sent_score

            # 综合评分 = 技术形态(50%) + 情绪信号(20%) + 因子评分(30%)
            if enable_factors and enable_sentiment:
                combined = tech_score * 0.5 + sent_score * 0.2 + factor_score * 0.3
            elif enable_factors:
                combined = tech_score * 0.6 + factor_score * 0.4
            elif enable_sentiment:
                combined = tech_score * 0.7 + sent_score * 0.3
            else:
                combined = tech_score

            if combined < min_score:
                continue

            # 信号名称（技术+情绪）
            all_names = TechnicalPatterns.signal_names(tech_signals) +                         SentimentSignals.signal_names(sent_signals)

            results.append({
                "stock_code": code,
                "short_name": row.get("short_name", ""),
                "price": float(row.get("price", 0)),
                "change_pct": float(row.get("change_pct", 0)),
                "volume": float(row.get("volume", 0)),
                "amount": float(row.get("amount", 0)),
                "signal_score": total_tech_score,
                "sentiment_score": round(sent_score, 1),
                "factor_score": round(factor_score, 1),
                "combined_score": round(combined, 1),
                "max_signal_level": max(
                    TechnicalPatterns.max_signal_level(tech_signals),
                    max((s["level"] for s in sent_signals), default=0),
                ),
                "signal_count": len(all_signals),
                "signal_names": " | ".join(all_names),
                "signals": all_signals,
                "factor_details": factor_details,
            })

        if not results:
            logger.info("扫描完成，无符合条件的股票")
            return pd.DataFrame()

        result_df = pd.DataFrame(results).sort_values("combined_score", ascending=False)
        if top_n > 0:
            result_df = result_df.head(top_n)

        logger.info("选股完成: %d 只", len(result_df))
        return result_df

    def screen_with_concepts(self, **kwargs) -> pd.DataFrame:
        """选股并补充概念信息"""
        df = self.screen(**kwargs)
        if df.empty:
            return df
        logger.info("补充概念信息...")
        concepts_list = []
        for code in df["stock_code"]:
            concepts = self.fetcher.stock_concepts(code)
            concepts_list.append(", ".join(concepts[:5]) if concepts else "")
        df["concepts"] = concepts_list
        return df

    def screen_with_capital_flow(self, **kwargs) -> pd.DataFrame:
        """选股并补充资金流向"""
        df = self.screen(**kwargs)
        if df.empty:
            return df
        logger.info("补充资金流向...")
        try:
            flow_df = self.fetcher.all_capital_flow(days_type=1)
            if flow_df is not None and not flow_df.empty:
                code_col = next((c for c in flow_df.columns if "code" in c.lower()), None)
                if code_col:
                    inflow_map = {}
                    for _, r in flow_df.iterrows():
                        code = str(r[code_col]).zfill(6)
                        # 找净流入列
                        for col in flow_df.columns:
                            if "net" in col.lower():
                                inflow_map[code] = r[col]
                                break
                    df["net_capital_inflow"] = df["stock_code"].map(inflow_map).fillna(0)
        except Exception as e:
            logger.warning("获取资金流向失败: %s", e)
        return df

    def _filter_market(self, df: pd.DataFrame, min_price=0, max_price=99999,
                       min_volume=0, min_turnover=0, min_market_cap=0, max_market_cap=999999,
                       exclude_st=True, exclude_beijing=False, exclude_chinext=True) -> pd.DataFrame:
        """应用过滤条件"""
        # 价格
        price_col = "price"
        if price_col in df.columns:
            df = df[df[price_col] >= min_price]
            df = df[df[price_col] <= max_price]

        # 成交额
        vol_col = "volume" if "volume" in df.columns else None
        if vol_col and min_volume > 0:
            df = df[df[vol_col] >= min_volume]

        # 排除 ST
        if exclude_st and "short_name" in df.columns:
            df = df[~df["short_name"].str.contains("ST|退", na=False)]

        # 排除北交所 (8xxxxx)
        if exclude_beijing and "stock_code" in df.columns:
            df = df[~df["stock_code"].str.startswith("8")]

        if exclude_chinext and "stock_code" in df.columns:
            df = df[~df["stock_code"].str.startswith("3")]
        # 始终排除科创板(688)、北交所(8)、B股(9)，与实时管道保持一致
        if "stock_code" in df.columns:
            df = df[~df["stock_code"].str.startswith("688")]
            df = df[~df["stock_code"].str.startswith("9")]  # B股
        return df

    def quick_scan(self, stock_code: str) -> dict:
        """快速扫描单只股票，返回技术信号"""
        from datetime import timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=DEFAULT_KLINE_DAYS)).strftime("%Y-%m-%d")
        kline = self.fetcher.kline(stock_code, start_date=start, end_date=end)
        if kline is None or kline.empty:
            return {"stock_code": stock_code, "error": "无K线数据"}
        tech_signals = TechnicalPatterns.scan(kline)
        tech_score = TechnicalPatterns.total_score(tech_signals)
        names = TechnicalPatterns.signal_names(tech_signals)

        sent_signals = SentimentSignals.scan(kline)
        sent_score = SentimentSignals.total_score(sent_signals)
        sent_names = SentimentSignals.signal_names(sent_signals)

        factor_score = 0.0
        factor_details = {}
        try:
            af = AlphaFactors(kline)
            factor_details = af.compute_all()
            factor_score = af.composite_score()
        except Exception:
            pass

        all_signals = tech_signals + sent_signals
        all_names = names + sent_names

        # 补充概念
        concepts = self.fetcher.stock_concepts(stock_code)

        # 均线
        close = kline["close"]
        ma5 = close.rolling(5).mean().iloc[-1] if len(kline) >= 5 else 0
        ma10 = close.rolling(10).mean().iloc[-1] if len(kline) >= 10 else 0
        ma20 = close.rolling(20).mean().iloc[-1] if len(kline) >= 20 else 0
        support = kline["low"].tail(20).min()
        resistance = kline["high"].tail(20).max()

        # 综合判断
        parts = []
        if all_signals:
            parts.append(f"识别到 {len(all_signals)} 个信号，技术{tech_score}+情绪{sent_score}")
        if concepts:
            parts.append(f"概念: {', '.join(concepts[:5])}")
        if ma5 > ma10 > ma20 > 0:
            parts.append("均线多头排列，趋势向上")
        elif ma5 < ma10 < ma20 and all(m > 0 for m in (ma5, ma10, ma20)):
            parts.append("均线空头排列，趋势偏弱")

        return {
            "stock_code": stock_code,
            "kline": kline,
            "tech_signals": tech_signals,
            "sent_signals": sent_signals,
            "tech_score": tech_score,
            "sentiment_score": sent_score,
            "factor_score": factor_score,
            "factor_details": factor_details,
            "all_signals": all_signals,
            "signal_names": all_names,
            "combined_score": round(tech_score * 0.5 + sent_score * 0.2 + factor_score * 0.3, 1),
            "concepts": concepts[:5],
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "support": support,
            "resistance": resistance,
            "summary": "; ".join(parts) or "无明显信号",
        }
