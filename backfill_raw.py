#!/usr/bin/env python3
# ============================================================
# backfill_raw.py — 把 claude.ai 导出的 conversations.json
# 逐字灌进原文保险箱（raw_events）。
#
# 两种跑法：
#   1) HTTP 模式（推荐，在你自己电脑上跑）：
#      python backfill_raw.py conversations.json \
#          --url https://<你的brain域名>/api/ingest-raw \
#          --token $OMBRE_INGEST_TOKEN
#   2) 直写模式（在服务器上、或本地先建库再上传）：
#      python backfill_raw.py conversations.json --db ./raw_events.sqlite
#
# 哈希 + source_event_id 双重去重：重复导入不会存两遍，放心多跑。
# ============================================================

import argparse
import json
import sys
import urllib.request

BATCH = 400


def _msg_text(msg: dict) -> str:
    """claude.ai 导出格式：优先拼 content 里的 text 块，退回顶层 text 字段。"""
    parts = []
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    if parts:
        return "\n".join(parts)
    return str(msg.get("text") or "")


def parse_claude_export(data) -> list[dict]:
    """conversations.json（对话数组）→ raw_events 事件列表。"""
    conversations = data if isinstance(data, list) else [data]
    events = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        conv_id = str(conv.get("uuid") or conv.get("id") or "")
        conv_name = str(conv.get("name") or "")
        for msg in conv.get("chat_messages", conv.get("messages", [])) or []:
            if not isinstance(msg, dict):
                continue
            text = _msg_text(msg).strip()
            if not text:
                continue
            sender = str(msg.get("sender") or msg.get("role") or "").lower()
            role = "user" if sender in ("human", "user") else "assistant"
            events.append({
                "source_event_id": str(msg.get("uuid") or msg.get("id") or ""),
                "role": role,
                "text": text,
                "created_at": str(msg.get("created_at") or msg.get("create_time") or ""),
                "conversation_id": conv_id,
                "metadata": {"conversation_name": conv_name} if conv_name else {},
            })
    return events


def push_http(url: str, token: str, events: list[dict], source: str) -> dict:
    totals = {"inserted": 0, "duplicate": 0, "rejected": 0}
    for i in range(0, len(events), BATCH):
        chunk = events[i:i + BATCH]
        body = json.dumps({"source": source, "events": chunk}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Ombre-Ingest-Token", token)
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        for k in totals:
            totals[k] += int(result.get(k, 0))
        done = min(i + BATCH, len(events))
        print(f"  {done}/{len(events)}  +{result.get('inserted', 0)} 新入 / {result.get('duplicate', 0)} 已有")
    return totals


def push_db(db_path: str, events: list[dict], source: str) -> dict:
    from raw_events import RawEventStore  # 与本脚本同目录即可
    store = RawEventStore({"raw_events": {"db_path": db_path}, "buckets_dir": "."})
    totals = {"inserted": 0, "duplicate": 0, "rejected": 0}
    for i in range(0, len(events), BATCH):
        result = store.ingest(events[i:i + BATCH], source=source)
        for k in totals:
            totals[k] += int(result.get(k, 0))
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="conversations.json → 原文保险箱")
    ap.add_argument("file", help="claude.ai 导出的 conversations.json 路径")
    ap.add_argument("--url", default="", help="/api/ingest-raw 完整地址（HTTP 模式）")
    ap.add_argument("--token", default="", help="X-Ombre-Ingest-Token（HTTP 模式）")
    ap.add_argument("--db", default="", help="直写 sqlite 路径（直写模式）")
    ap.add_argument("--source", default="claude-export", help="来源标签，默认 claude-export")
    args = ap.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)
    events = parse_claude_export(data)
    print(f"解析出 {len(events)} 条消息。")
    if not events:
        return 0

    if args.db:
        totals = push_db(args.db, events, args.source)
    elif args.url:
        totals = push_http(args.url, args.token, events, args.source)
    else:
        print("要么给 --url，要么给 --db。", file=sys.stderr)
        return 2

    print(f"完成：新入 {totals['inserted']}，已有跳过 {totals['duplicate']}，拒收 {totals['rejected']}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
