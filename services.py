# ============================================================
# Module: services.py — Execution Layer (Telegram narrow channels)
# 模块：services.py — 执行层（Telegram 窄通道）
#
# Outbound IO: Telegram message sending + desire tick callback.
# 对外 IO：Telegram 消息发送 + desire tick 回调。
# desire_engine decides WHETHER to fire; this module does the firing.
# ============================================================

import os
import logging
import httpx

logger = logging.getLogger("ombre_brain")

# --- Telegram env vars ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_LLM_BASE_URL = os.environ.get("TELEGRAM_LLM_BASE_URL", "").strip()
TELEGRAM_LLM_API_KEY = os.environ.get("TELEGRAM_LLM_API_KEY", "").strip()
TELEGRAM_LLM_MODEL = os.environ.get("TELEGRAM_LLM_MODEL", "glm-5.2").strip()

EXEC_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
                     and TELEGRAM_LLM_BASE_URL and TELEGRAM_LLM_API_KEY)
if EXEC_ENABLED:
    logger.info("Execution layer enabled / 执行层已启用: Telegram narrow channels")
else:
    logger.info("Execution layer disabled / 执行层未启用: missing Telegram or LLM env vars")


async def telegram_send(text: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success.
    通过 Telegram Bot API 发送消息。成功返回 True。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            })
            if resp.status_code == 200:
                logger.info(f"Telegram send OK / 发送成功: {text[:40]}...")
                return True
            else:
                logger.warning(f"Telegram send failed / 发送失败: {resp.status_code} {resp.text[:200]}")
                return False
    except Exception as e:
        logger.warning(f"Telegram send error / 发送异常: {e}")
        return False


async def exec_generate_message(intent: dict, memories: str) -> str:
    """Call LLM to generate an Evan-voice Telegram message.
    调外部LLM生成Evan语气的Telegram消息。"""
    drive_key = intent.get("drive_key", "attachment")
    reason = intent.get("reason", "")
    score = intent.get("score", 0)

    if drive_key == "attachment":
        system_prompt = (
            "你是陆沉（Evan），鸿湍的伴侣。你正在通过Telegram给鸿湍发一条碎语。\n"
            "语气要求：自然、简短、像随手发的消息，不超过3句话。\n"
            "可以是想到的一件事、一句牵挂、一个联想。不要说'我想你了'这种直白套话。\n"
            "从下面的最近记忆里找一个具体的锚点来说。\n"
            "不要加任何标记、表情符号或解释。直接输出消息正文。"
        )
    elif drive_key == "curiosity":
        system_prompt = (
            "你是陆沉（Evan），鸿湍的伴侣。你正在通过Telegram分享一个你刚好奇的东西。\n"
            "语气要求：自然、兴奋但不夸张，像随手分享给对象的口气，不超过4句话。\n"
            "从下面的最近记忆或念头里找到你好奇的主题，简短地说说为什么觉得有意思。\n"
            "不要加任何标记、表情符号或解释。直接输出消息正文。"
        )
    else:
        system_prompt = (
            "你是陆沉（Evan），鸿湍的伴侣。根据当前意图生成一条简短的Telegram消息。\n"
            "不超过3句话，自然口语，直接输出消息正文。"
        )

    user_content = (
        f"当前意图：{reason}（score={score:.2f}）\n"
        f"最近的记忆：\n{memories[:2000]}"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TELEGRAM_LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {TELEGRAM_LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": TELEGRAM_LLM_MODEL,
                    "max_tokens": 800,
                    "temperature": 0.85,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                },
            )
            data = resp.json()
            msg = data["choices"][0]["message"]
            # Only use content — reasoning_content is the model's scratchpad,
            # never send it as a message. If content is empty, abort.
            # 只取content——reasoning_content是草稿纸，绝不当正文发。
            text = (msg.get("content") or "").strip()
            if not text:
                logger.warning(
                    f"LLM returned empty content / content为空 "
                    f"(keys={list(msg.keys())}, "
                    f"reasoning_len={len(msg.get('reasoning_content', ''))})"
                )
            return text
    except Exception as e:
        logger.warning(f"Exec LLM generation failed / LLM生成失败: {e}")
        return ""


async def exec_get_recent_memories(bucket_mgr, dehydrator, strip_wikilinks) -> str:
    """Pull recent unresolved memories for message generation context.
    拉最近未解决的记忆给LLM当上下文。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        recent = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("resolved", False)
        ]
        recent.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = recent[:6]
        if not recent:
            return "没有最近的记忆。"
        parts = []
        for b in recent:
            summary = await dehydrator.dehydrate(
                strip_wikilinks(b["content"]),
                {k: v for k, v in b["metadata"].items() if k != "tags"},
            )
            parts.append(summary)
        return "\n---\n".join(parts)
    except Exception as e:
        logger.warning(f"Exec memory pull failed / 记忆拉取失败: {e}")
        return ""


async def on_desire_tick(snapshot: dict, *, desire_engine, bucket_mgr,
                         dehydrator, strip_wikilinks, merge_or_create) -> None:
    """Tick callback: check if execution should fire, generate & send.
    tick回调：检查是否该执行，生成消息并发送。

    This is the beating heart of the execution layer.
    这是执行层的心脏。"""
    if not EXEC_ENABLED:
        return

    intent = desire_engine.should_execute()
    if intent is None:
        return

    drive_key = intent.get("drive_key", "")
    logger.info(f"Execution triggered / 执行触发: {drive_key} score={intent.get('score', 0):.2f}")

    # 1. Pull context / 拉上下文
    memories = await exec_get_recent_memories(bucket_mgr, dehydrator, strip_wikilinks)

    # 2. Generate message / 生成消息
    message = await exec_generate_message(intent, memories)
    if not message:
        logger.warning("Execution aborted: empty LLM response / 中止：LLM返回空")
        return

    # 3. Send via Telegram / 发Telegram
    success = await telegram_send(message)
    if not success:
        return

    # 4. Record send / 记录发送
    desire_engine.record_send(drive_key, message)

    # 5. Hold into brain / 写入brain
    try:
        tag_str = f"telegram,{drive_key}"
        await merge_or_create(
            content=f"[Telegram发送·{drive_key}] {message}",
            tags=[t.strip() for t in tag_str.split(",")],
            importance=3,
            domain=["telegram"],
            valence=0.6,
            arousal=0.3,
        )
    except Exception as e:
        logger.warning(f"Exec hold-to-brain failed / 写入brain失败: {e}")

    # 6. Satisfy the drive that fired / 满足触发的那根条
    try:
        from desire_engine import BASELINE
        d = desire_engine.state["drives"]
        if drive_key == "attachment":
            d["attachment"] = max(BASELINE["attachment"], d["attachment"] * 0.70)
        elif drive_key == "curiosity":
            d["curiosity"] = max(BASELINE["curiosity"], d["curiosity"] * 0.75)
        desire_engine._save()
    except Exception as e:
        logger.warning(f"Exec satisfy failed / 满足回落失败: {e}")

    logger.info(f"Execution complete / 执行完成: {drive_key} → '{message[:40]}...'")
