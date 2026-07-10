#!/usr/bin/env python3
"""
每日选股评分 — 开盘后运行一次，生成日级别评分缓存

供盘中选股引擎（stock_selector.py）加载合并。

用法:
  python3 daily_scorer.py                  # 扫描全市场并缓存
  python3 daily_scorer.py --top 50         # 只排前 50 只
  python3 daily_scorer.py --status         # 查看缓存状态
  python3 daily_scorer.py --clear          # 清空当日缓存
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
CACHE_PREFIX = "daily_scores_"
MAX_CACHE_FILES = 5              # 保留最近 N 个交易日的缓存


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def cache_path(date_str: str = None) -> Path:
    return DATA_DIR / f"{CACHE_PREFIX}{date_str or _today()}.json"


def load_daily_scores() -> dict:
    """加载当日评分缓存，返回 {stock_code: {...}}。"""
    cp = cache_path()
    if not cp.exists():
        logger.info("\u5f53\u65e5\u8bc4\u5206\u7f13\u5b58\u4e0d\u5b58\u5728: %s", cp)
        return {}
    try:
        with open(cp, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("\u52a0\u8f7d\u65e5\u8bc4\u5206\u7f13\u5b58: %d \u53ea\u80a1\u7968", len(data))
        return data
    except Exception as e:
        logger.warning("\u52a0\u8f7d\u65e5\u8bc4\u5206\u7f13\u5b58\u5931\u8d25: %s", e)
        return {}
def cleanup_old_caches(keep: int = MAX_CACHE_FILES):
    """删除超过 N 个交易日以前的评分缓存，只保留最近的。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(CACHE_PREFIX)}(\d{{8}})\.json$")
    caches = []
    for f in DATA_DIR.iterdir():
        m = pattern.match(f.name)
        if m:
            caches.append((m.group(1), f))

    if len(caches) <= keep:
        return

    # 按日期排序，保留最新的 keep 个
    caches.sort(key=lambda x: x[0], reverse=True)
    for date_str, f in caches[keep:]:
        try:
            f.unlink()
            logger.info("清理过期缓存: %s", f.name)
        except OSError as e:
            logger.warning("清理缓存失败 %s: %s", f.name, e)



def run_scan(top_n: int = 300) -> dict:
    """执行全市场扫描并写入缓存。"""
    from layer1_data import DataFetcher
    from layer2_scan import StockScreener

    logger.info("\u5f00\u59cb\u6bcf\u65e5\u8bc4\u5206\u626b\u63cf...")

    fetcher = DataFetcher()
    screener = StockScreener(fetcher=fetcher)

    result = screener.screen(
        min_price=3.0, max_price=100.0,
        exclude_st=True, exclude_chinext=False,
        top_n=top_n, batch_size=50,
    )

    if result is None or result.empty:
        logger.warning("\u6bcf\u65e5\u8bc4\u5206\u626b\u63cf\u65e0\u7ed3\u679c")
        return {}

    scores = {}
    for _, row in result.iterrows():
        code = str(row.get("stock_code", "")).strip()
        if not code:
            continue
        scores[code] = {
            "combined_score": round(float(row.get("combined_score", 0)), 1),
            "signal_score": float(row.get("signal_score", 0)),
            "sentiment_score": float(row.get("sentiment_score", 0)),
            "factor_score": round(float(row.get("factor_score", 0)), 1),
            "signal_count": int(row.get("signal_count", 0)),
            "signal_names": str(row.get("signal_names", "")),
        }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cp = cache_path()
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)

    logger.info("\u6bcf\u65e5\u8bc4\u5206\u5b8c\u6210: %d \u53ea\u80a1\u7968, \u5df2\u7f13\u5b58\u81f3 %s",
                len(scores), cp)
    cleanup_old_caches()
    return scores


def cmd_scan(args):
    run_scan(top_n=args.top if args.top is not None else 300)


def cmd_status(args):
    cp = cache_path()
    if not cp.exists():
        print(f"\u5f53\u65e5\u7f13\u5b58\u4e0d\u5b58\u5728: {cp}")
        return
    data = load_daily_scores()
    if not data:
        print(f"\u7f13\u5b58\u6587\u4ef6\u5b58\u5728\u4f46\u6570\u636e\u4e3a\u7a7a: {cp}")
        return
    stats = {
        "date": _today(),
        "total": len(data),
        "avg_combined": round(sum(v["combined_score"] for v in data.values()) / len(data), 1),
        "avg_signals": round(sum(v["signal_count"] for v in data.values()) / len(data), 1),
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    top = sorted(data.items(), key=lambda x: x[1]["combined_score"], reverse=True)[:5]
    print(f"\n\u7efc\u5408\u8bc4\u5206 Top 5:")
    for code, v in top:
        print(f"  {code}  \u7efc\u5408{v['combined_score']}  \u4fe1\u53f7{v['signal_count']}\u4e2a  {v['signal_names'][:50]}")
    print(f"\n\u7f13\u5b58\u6587\u4ef6: {cp}")


def cmd_clear(args):
    cp = cache_path()
    if cp.exists():
        cp.unlink()
        print(f"\u5df2\u6e05\u7a7a\u7f13\u5b58: {cp}")
    else:
        print("\u7f13\u5b58\u4e0d\u5b58\u5728")


def main():
    parser = argparse.ArgumentParser(description="\u6bcf\u65e5\u9009\u80a1\u8bc4\u5206\u7f13\u5b58\u5de5\u5177")
    parser.add_argument("--top", type=int, default=300, help="\u8f93\u51fa\u524d N \u53ea (0=\u5168\u91cf)")
    parser.add_argument("--status", action="store_true", help="\u67e5\u770b\u7f13\u5b58\u72b6\u6001")
    parser.add_argument("--clear", action="store_true", help="\u6e05\u7a7a\u5f53\u65e5\u7f13\u5b58")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("urllib3", "requests", "urllib3.connectionpool", "adata"):
        logging.getLogger(name).setLevel(logging.WARNING)

    if args.status:
        cmd_status(args)
    elif args.clear:
        cmd_clear(args)
    else:
        cmd_scan(args)


if __name__ == "__main__":
    main()
