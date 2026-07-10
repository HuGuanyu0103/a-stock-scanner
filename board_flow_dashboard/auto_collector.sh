#!/bin/bash
# 板块资金流向自动采集器管理脚本
# 用法: ./auto_collector.sh start | stop | status | backfill

APP_DIR="/Users/hoo/Documents/a股"
APP_FILE="$APP_DIR/board_flow_dashboard/app.py"
LOG_FILE="/tmp/a股_app.log"
PID_FILE="/tmp/a股_app.pid"
BACKFILL_LOG="/tmp/a股_backfill.log"

start_app() {
    if pgrep -f "app.py" > /dev/null 2>&1; then
        echo "[$(date '+%H:%M:%S')] 采集器已在运行，跳过启动" | tee -a "$LOG_FILE"
        return 0
    fi

    echo "[$(date '+%H:%M:%S')] 启动采集器..." | tee -a "$LOG_FILE"
    cd "$APP_DIR"
    nohup python3 "$APP_FILE" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 5

    if pgrep -f "app.py" > /dev/null 2>&1; then
        echo "[$(date '+%H:%M:%S')] 采集器启动成功" | tee -a "$LOG_FILE"
        # 自动跑日评分（大盘数据就绪后）
        echo "[$(date '+%H:%M:%S')] 执行日评分扫描..." | tee -a "$LOG_FILE"
        cd "$APP_DIR"
        python3 -c "
import sys; sys.path.insert(0,'.')
from board_flow_dashboard.daily_scorer import run_scan
scores = run_scan(top_n=0)
print(f'日评分完成: {len(scores)} 只股票')
" >> "$LOG_FILE" 2>&1 &
    else
        echo "[$(date '+%H:%M:%S')] 采集器启动失败！" | tee -a "$LOG_FILE"
        return 1
    fi
}

stop_app() {
    if ! pgrep -f "app.py" > /dev/null 2>&1; then
        echo "[$(date '+%H:%M:%S')] 采集器未运行" | tee -a "$LOG_FILE"
        return 0
    fi

    echo "[$(date '+%H:%M:%S')] 停止采集器..." | tee -a "$LOG_FILE"
    pkill -f "app.py" 2>/dev/null
    sleep 2

    # 确保进程终止
    if pgrep -f "app.py" > /dev/null 2>&1; then
        pkill -9 -f "app.py" 2>/dev/null
        sleep 1
    fi

    if pgrep -f "app.py" > /dev/null 2>&1; then
        echo "[$(date '+%H:%M:%S')] 采集器停止失败！" | tee -a "$LOG_FILE"
        return 1
    else
        echo "[$(date '+%H:%M:%S')] 采集器已停止" | tee -a "$LOG_FILE"
    fi
}

backfill_now() {
    # 收盘后补抓当日最后数据（采集器不在运行状态下用）
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行补抓..." | tee -a "$BACKFILL_LOG"
    cd "$APP_DIR"

    # 如果采集器在跑，直接用；否则独立调 API
    python3 -c "
import sys
sys.path.insert(0, '.')
from board_flow_dashboard.data_fetcher import fetch_all_sectors_snapshot, fetch_industry_sectors_snapshot
from board_flow_dashboard.collector import Storage
from pathlib import Path
from datetime import datetime

today = datetime.now().strftime('%Y-%m-%d')
storage = Storage(Path('board_flow_dashboard/data/collector.db'))

for name, fn in [('概念', fetch_all_sectors_snapshot), ('行业', fetch_industry_sectors_snapshot)]:
    try:
        result = fn(timeout=15.0)
        if result and result.get('sectors'):
            if name == '概念':
                storage.save_concept_snapshot(today, result['time'], result)
            else:
                storage.save_industry_snapshot(today, result['time'], result)
            print(f'{name}板块: {len(result[\"sectors\"])}个 时间{result[\"time\"]}')
        else:
            print(f'{name}板块: 获取失败')
    except Exception as e:
        print(f'{name}板块: 异常 {e}')
" >> "$BACKFILL_LOG" 2>&1
    echo "[$(date '+%H:%M:%S')] 补抓完成" | tee -a "$BACKFILL_LOG"
}

case "${1:-status}" in
    start)
        start_app
        ;;
    stop)
        stop_app
        ;;
    restart)
        stop_app
        sleep 2
        start_app
        ;;
    backfill)
        backfill_now
        ;;
    status|*)
        if pgrep -f "app.py" > /dev/null 2>&1; then
            echo "[$(date '+%H:%M:%S')] 采集器运行中"
            # 检查健康状态
            curl -s 'http://127.0.0.1:8080/api/status' 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(f'  快照: 概念{d.get(\"snapshots_concept\",0)} 行业{d.get(\"snapshots_industry\",0)} 数据日期{d.get(\"data_date\",\"?\")}')
except:
    print('  无法获取状态')
"
        else
            echo "[$(date '+%H:%M:%S')] 采集器未运行"
        fi
        ;;
esac
