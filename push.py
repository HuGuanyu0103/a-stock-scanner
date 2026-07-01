#!/usr/bin/env python3
"""
微信推送工具 — 支持企业微信群机器人 + Server酱

企业微信（推荐，消息直接到群聊天）:
  1. 建一个只有你自己的群
  2. 群设置 → 群机器人 → 添加
  3. 复制 Webhook URL
  4. 配置到 config.yaml

使用方式:
  python push.py "消息内容"
  python push.py --title "标题" "内容"
  python push.py --file report.md
  python push.py --test                     # 测试推送
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml


def load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


def get_send_key(config: dict | None = None) -> str:
    config = config or load_config()
    return config.get("serverchan", {}).get("send_key", "") or os.environ.get("SERVERCHAN_KEY", "")


def get_webhook_url(config: dict | None = None) -> str:
    config = config or load_config()
    return config.get("wecom", {}).get("webhook_url", "") or os.environ.get("WECOM_WEBHOOK_URL", "")


def send(title: str, content: str, config: dict | None = None) -> dict:
    """自动选择推送渠道：企业微信 > Server酱"""
    config = config or load_config()
    wecom_url = get_webhook_url(config)
    if wecom_url:
        return send_wecom(title, content, wecom_url)
    return send_serverchan(title, content, get_send_key(config))


def send_wecom(title: str, content: str, webhook_url: str) -> dict:
    """企业微信群机器人推送"""
    full_text = f"**{title}**\n\n{content}" if title else content
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": full_text}
    }).encode("utf-8")
    try:
        req = urllib.request.Request(webhook_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") == 0:
                return {"code": 0, "message": "ok"}
            return {"code": result.get("errcode", -1), "message": result.get("errmsg", "?")}
    except urllib.error.HTTPError as e:
        return {"code": e.code, "message": e.read().decode()[:200]}
    except Exception as e:
        return {"code": -1, "message": str(e)}


def send_serverchan(title: str, content: str, send_key: str) -> dict:
    """Server酱推送"""
    if not send_key:
        return {"code": -1, "message": "未配置 SendKey"}
    url = f"https://sctapi.ftqq.com/{send_key}.send"
    data = urllib.parse.urlencode({"title": title[:256], "content": content}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"code": -1, "message": f"Server酱: {e}"}


def main():
    parser = argparse.ArgumentParser(description="推送消息到微信")
    parser.add_argument("message", nargs="?", default="", help="消息内容")
    parser.add_argument("--title", "-t", default="\U0001f4e9 来自 Codex 的消息", help="消息标题")
    parser.add_argument("--file", "-f", default="", help="从文件读取消息内容")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--test", action="store_true", help="发送测试消息")
    args = parser.parse_args()

    config = load_config(args.config)
    wecom_url = get_webhook_url(config)
    server_key = get_send_key(config)

    if not wecom_url and not server_key:
        print("\u274c 未配置推送渠道")
        print("")
        print("企业微信（推荐，消息直达群聊天）:")
        print("  1. 企业微信 \u2192 建一个群 \u2192 群设置 \u2192 群机器人 \u2192 添加")
        print("  2. 复制 Webhook URL")
        print("  3. 在 config.yaml 中添加:")
        print("     wecom:")
        print('       webhook_url: "你的webhook地址"')
        sys.exit(1)

    if args.test:
        r = send("推送测试", "如果你看到这条消息，配置成功！", config=config)
        if r.get("code") == 0:
            ch = "企业微信" if get_webhook_url(config) else "Server酱"
            print(f"\u2705 测试推送成功（{ch}），请查看消息")
        else:
            print(f"\u274c 推送失败: {r.get('message')}")
        return

    content = args.message
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            content = f.read()
    elif not content and not sys.stdin.isatty():
        content = sys.stdin.read().strip()
    elif not content:
        parser.print_help()
        sys.exit(1)

    result = send(title=args.title, content=content, config=config)
    if result.get("code") == 0:
        ch = "企业微信" if get_webhook_url(config) else "Server酱"
        print(f"\u2705 推送成功: {args.title} ({ch})")
    else:
        print(f"\u274c 推送失败: {result.get('message', '未知错误')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
