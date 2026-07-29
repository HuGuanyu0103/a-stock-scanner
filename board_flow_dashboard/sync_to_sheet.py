#!/usr/bin/env python3
"""
决策飞轮实盘结算数据 → 飞书表格 自动同步脚本

把 decision_store 里真实已结算的逐笔数据，同步到 PRD 引用的飞书数据源表
「实盘结算(脚本自动更新)」sheet。可由 cron 定时运行，实现"实盘数据自动更新"。

诚信保证：本脚本只搬运 decision_store 里真实结算的数据，不生成/不编造任何数字。
若库为空，则表内数据区保持为空（只有表头），如实反映"样本积累中"。

用法：
  # 一次性同步（需已配置飞书 user 身份，见 lark-cli 鉴权）
  python3 sync_to_sheet.py

  # 环境变量可覆盖默认表 token / sheet id
  SHEET_TOKEN=xxx SHEET_ID=xxx python3 sync_to_sheet.py

  # 定时自动更新（示例 crontab：每交易日 16:30 同步一次）
  # 30 16 * * 1-5  cd /path/to/board_flow_dashboard && python3 sync_to_sheet.py >> sync.log 2>&1
"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_to_sheet")

# PRD 引用的「规则引擎实验数据源表(实盘)」；可用环境变量覆盖
SHEET_TOKEN = os.environ.get("SHEET_TOKEN", "NDsEwvxvfiCM8kki8qXcACU3npd")
SHEET_ID = os.environ.get("SHEET_ID", "9d9bf6")  # 「实盘结算(脚本自动更新)」sheet
DATA_START_CELL = "A4"   # 表头占 1-3 行(标题/说明/列名)，数据从第 4 行起
MAX_ROWS = 500           # 单次同步最多写入行数（防表爆）

HEADER = ["结算日期", "板块-信号组合", "池(A/B)", "买入价", "结算价",
          "收益率%(扣0.1%成本)", "同期沪深300%", "超额alpha%", "累计样本n", "当前乘数"]


def _fetch_settled_rows() -> list[list]:
    """从 decision_store 拉真实已结算逐笔明细 + 从 weight_tuner 取当前乘数。

    返回 CSV 行(list of list)；库为空则返回 []。全部真实数据，无编造。
    """
    try:
        from decision_store import get_decision_store
        from weight_tuner import get_multipliers
    except ImportError:
        logger.error("需在 board_flow_dashboard 目录下运行（import decision_store 失败）")
        return []

    store = get_decision_store()
    mults = {}
    try:
        mults = get_multipliers(store=store) or {}
    except Exception as e:
        logger.warning("取乘数失败(不阻断): %s", e)

    # 逐笔明细：直接查库拿全字段（get_settled_trades 字段不够）
    rows = []
    try:
        import sqlite3
        from decision_store import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        # 合并 decisions(closed) + shadow(resolved)；shadow 用 recommend_price 当买入价
        q_dec = ("SELECT exit_date, signal_combo, pool, entry_price, exit_price, return_pct "
                 "FROM decisions WHERE status='closed' AND return_pct IS NOT NULL AND exit_date!=''")
        q_shadow = ("SELECT exit_date, signal_combo, pool, recommend_price AS entry_price, "
                    "exit_price, return_pct FROM shadow_decisions "
                    "WHERE status='resolved' AND return_pct IS NOT NULL AND exit_date!=''")
        recs = [dict(r) for r in conn.execute(q_dec).fetchall()]
        recs += [dict(r) for r in conn.execute(q_shadow).fetchall()]
        conn.close()
    except Exception as e:
        logger.error("查库失败: %s", e)
        return []

    recs.sort(key=lambda x: x.get("exit_date") or "")
    # 累计样本 n：按 signal_combo 计数
    combo_n = {}
    for r in recs[:MAX_ROWS]:
        combo = r.get("signal_combo") or ""
        combo_n[combo] = combo_n.get(combo, 0) + 1
        ret = r.get("return_pct")
        rows.append([
            r.get("exit_date", ""), combo, (r.get("pool") or "").upper(),
            r.get("entry_price", ""), r.get("exit_price", ""),
            round(ret, 2) if ret is not None else "",
            "", "",  # 同期沪深300 / alpha：需基准对齐，留待 get_excess_return 扩展
            combo_n[combo],
            mults.get(combo, ""),
        ])
    return rows


def _push_to_sheet(rows: list[list]) -> bool:
    """用 lark-cli csv-put 把数据行写入飞书表（数据区，不动表头）。"""
    if not rows:
        logger.info("无已结算样本，实盘表数据区保持为空（如实反映样本积累中）")
        return True
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    cmd = [
        "lark-cli", "sheets", "+csv-put",
        "--spreadsheet-token", SHEET_TOKEN,
        "--sheet-id", SHEET_ID,
        "--start-cell", DATA_START_CELL,
        "--csv", "-",  # 从 stdin
        "--as", "user",
    ]
    env = dict(os.environ, LARK_CLI_NO_PROXY="1")
    try:
        r = subprocess.run(cmd, input=buf.getvalue(), capture_output=True,
                           text=True, env=env, timeout=60)
    except Exception as e:
        logger.error("调用 lark-cli 失败: %s", e)
        return False
    ok = '"ok": true' in r.stdout or '"ok":true' in r.stdout
    if ok:
        logger.info("已同步 %d 行真实结算数据到实盘表", len(rows))
    else:
        logger.error("写入失败: %s", (r.stdout or r.stderr)[:300])
    return ok


def main() -> int:
    rows = _fetch_settled_rows()
    logger.info("从 decision_store 取到 %d 笔真实已结算样本", len(rows))
    ok = _push_to_sheet(rows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
