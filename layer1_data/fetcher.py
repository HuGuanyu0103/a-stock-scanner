"""
数据层 — 封装 adata + 腾讯 QQ K线备选
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_NUM_COLS = ("price", "change", "change_pct", "volume", "amount")


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    if df is not None and not df.empty:
        df.columns = [c.lower() for c in df.columns]
    return df


class DataFetcher:
    """统一数据获取接口，East Money + QQ 双源"""

    def __init__(self, max_workers: int = 4):
        self._max_workers = max_workers
        self._cached_codes = None

    # ── 股票基本信息 ─────────────────────────────────────────────

    def all_stocks(self) -> pd.DataFrame:
        """获取全市场股票代码列表"""
        if self._cached_codes is not None:
            return self._cached_codes

        cache_path = os.path.expanduser(
            "~/Library/Python/3.9/lib/python/site-packages/adata/stock/cache/code.csv"
        )
        if os.path.exists(cache_path):
            df = pd.read_csv(cache_path)
            df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
            df = df[["stock_code", "short_name"]]
            df = df[~df["short_name"].str.contains("退|B股", na=False)]
            self._cached_codes = df.reset_index(drop=True)
            logger.info("读取本地股票缓存: %d 只", len(self._cached_codes))
            return self._cached_codes

        try:
            import adata
            df = adata.stock.info.all_code()
            df = _normalize_cols(df)
            self._cached_codes = df
            return df
        except Exception as e:
            logger.warning("获取股票列表失败: %s", e)
            return pd.DataFrame()

    def stock_shares(self, stock_code: str) -> Optional[dict]:
        """获取总股本/流通A股（用于计算换手率）。缓存结果。"""
        cache_key = f"_shares_{stock_code}"
        if hasattr(self, cache_key):
            return getattr(self, cache_key)
        import adata
        try:
            df = adata.stock.info.get_stock_shares(stock_code=stock_code)
            if df is not None and not df.empty:
                latest = df.iloc[0]  # 第一行是最新数据
                result = {
                    "total_shares": int(latest.get("total_shares", 0)),
                    "list_a_shares": int(latest.get("list_a_shares", 0)),
                }
                setattr(self, cache_key, result)
                return result
        except Exception as e:
            logger.debug("获取股本失败 %s: %s", stock_code, e)
        return None

    def turnover_rate(self, stock_code: str) -> Optional[float]:
        """计算实时换手率 = 当日成交量 / 流通A股 * 100"""
        shares = self.stock_shares(stock_code)
        if not shares or shares["list_a_shares"] <= 0:
            return None
        try:
            mk = self.current_market([stock_code])
            if mk is not None and not mk.empty:
                vol = int(mk.iloc[0].get("volume", 0))
                if vol > 0:
                    return round(vol / shares["list_a_shares"] * 100, 2)
        except Exception as e:
            logger.debug("计算换手率失败 %s: %s", stock_code, e)
        return None

    def stock_concepts(self, stock_code: str) -> list:
        import adata
        try:
            df = adata.stock.info.get_concept_east(stock_code=stock_code)
            if df is None or df.empty:
                return []
            name_col = next((c for c in ("concept_name", "name", "concept") if c in df.columns), "")
            return df[name_col].tolist() if name_col else []
        except Exception:
            return []

    # ── 行情数据 ─────────────────────────────────────────────────

    def current_market(self, code_list: Optional[list] = None) -> pd.DataFrame:
        """获取实时行情 — adata(push2) 优先，失败降级到腾讯 qt.gtimg.cn"""
        # Primary: adata
        try:
            import adata
            df = adata.stock.market.list_market_current(code_list=code_list)
            df = _normalize_cols(df)
            if df is not None and not df.empty:
                for col in _NUM_COLS:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                return df
        except Exception as e:
            logger.debug("adata current_market 失败，降级腾讯: %s", e)

        # Fallback: Tencent API
        if code_list:
            try:
                return self._tencent_market(code_list)
            except Exception as e2:
                logger.warning("腾讯行情降级也失败: %s", e2)

        return pd.DataFrame()

    def _tencent_market(self, code_list: list) -> pd.DataFrame:
        """腾讯 qt.gtimg.cn API 获取实时行情（push2 降级方案）。"""
        import requests as req
        tencent_codes = []
        for c in code_list:
            c = str(c).zfill(6)
            prefix = "sh" if c.startswith("6") else "sz"
            tencent_codes.append(f"{prefix}{c}")
        url = f"http://qt.gtimg.cn/q={','.join(tencent_codes)}"
        resp = req.get(url, timeout=5)
        resp.encoding = 'gbk'
        rows = []
        for line in resp.text.strip().split(';\n'):
            if not line.strip() or '=' not in line:
                continue
            _, value = line.split('=', 1)
            value = value.strip().strip('"').strip("'")
            fields = value.split('~')
            if len(fields) < 33:
                continue
            rows.append({
                "stock_code": fields[2],
                "short_name": fields[1],
                "price": float(fields[3]) if fields[3] else 0,
                "change_pct": float(fields[32]) if fields[32] else 0,
                "change": float(fields[31]) if fields[31] else 0,
                "volume": int(fields[6]) * 100 if fields[6] else 0,  # 腾讯单位是手→股
                "amount": float(fields[37]) * 10000 if len(fields) > 37 and fields[37] else 0,
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def kline(self, stock_code: str, start_date="1990-01-01",
              end_date=None, k_type=1) -> pd.DataFrame:
        """获取个股历史 K 线 — East Money 优先，失败则降级到 QQ"""
        import adata
        end = end_date or datetime.now().strftime("%Y-%m-%d")

        # 尝试 East Money
        try:
            df = adata.stock.market.get_market(
                stock_code=stock_code, start_date=start_date,
                end_date=end, k_type=k_type,
            )
            if df is not None and not df.empty:
                return _normalize_cols(df)
        except Exception:
            pass

        # 降级到 QQ 接口
        logger.debug("East Money 失败，降级到 QQ: %s", stock_code)
        return self._qq_kline(stock_code, start_date, end, k_type)

    def _qq_kline(self, stock_code: str, start_date: str, end_date: str,
                  k_type: int) -> pd.DataFrame:
        """腾讯 QQ 接口获取 K 线（备选方案）"""
        import requests as req

        market = "sh" if stock_code.startswith("6") else "sz"
        url = f"http://ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{stock_code},day,,,450,qfq"
        try:
            r = req.get(url, timeout=10)
            data = r.json()
            if data.get("code") != 0:
                return pd.DataFrame()

            key = f"{market}{stock_code}"
            klines = data.get("data", {}).get(key, {}).get("qfqday", [])
            if not klines:
                return pd.DataFrame()

            rows = []
            for k in klines:
                rows.append({
                    "trade_date": k[0],
                    "open": float(k[1]),
                    "close": float(k[2]),
                    "high": float(k[3]),
                    "low": float(k[4]),
                    "volume": float(k[5]) * 10000,  # QQ 单位是万股
                    "stock_code": stock_code,
                })

            df = pd.DataFrame(rows)

            # 计算涨跌幅和成交额（近似）
            df["pre_close"] = df["close"].shift(1)
            df["change_pct"] = (df["close"] - df["pre_close"]) / df["pre_close"] * 100
            df["amount"] = df["volume"] * df["close"]  # 成交额≈成交量×收盘价
            df["change"] = df["close"] - df["pre_close"]

            df = df.dropna(subset=["close"])
            df = df.sort_values("trade_date").reset_index(drop=True)

            # 按日期过滤
            if start_date:
                df = df[df["trade_date"] >= start_date]
            if end_date:
                df = df[df["trade_date"] <= end_date]

            return df

        except Exception as e:
            logger.debug("QQ K线获取失败 %s: %s", stock_code, e)
            return pd.DataFrame()

    def batch_kline(self, stock_codes: list, days=120, k_type=1) -> dict:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        result = {}

        def fetch(code: str):
            try:
                df = self.kline(code, start_date=start, end_date=end, k_type=k_type)
                if df is not None and not df.empty:
                    return code, df
            except Exception:
                pass
            return code, None

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(fetch, c): c for c in stock_codes}
            for f in as_completed(futures):
                code, df = f.result()
                if df is not None:
                    result[code] = df
        return result

    # ── 资金流向 ─────────────────────────────────────────────────

    def all_capital_flow(self, days_type=1) -> pd.DataFrame:
        import adata
        try:
            df = adata.stock.market.all_capital_flow_east(days_type=days_type)
            return _normalize_cols(df)
        except Exception as e:
            logger.warning("获取资金流向失败: %s", e)
            return pd.DataFrame()

    # ── 指数 ─────────────────────────────────────────────────────

    def current_index(self, index_code="000001") -> dict:
        import adata
        return adata.stock.market.get_market_index_current(index_code=index_code)


if __name__ == "__main__":
    # 快速测试
    import warnings
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    f = DataFetcher()
    df = f.kline("000001", start_date="2025-06-01")
    if df is not None and not df.empty:
        print(f"K线获取成功: {len(df)} 条")
        print(df[["trade_date", "close", "volume"]].tail(3))
    else:
        print("K线获取失败")
