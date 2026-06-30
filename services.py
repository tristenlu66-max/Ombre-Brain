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
import time
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


# =============================================================
# Wander execution layer / 漫步执行层
# =============================================================
from wander_engine import (
    should_wander, pick_seed_bucket, pick_next_bucket,
    build_first_step_prompt, build_step_prompt, is_dead_end,
    format_wander_product, format_wander_summary,
    default_wander_state, WANDER_SYSTEM_PROMPT, WANDER_PARAMS,
)

# Wander uses the same DeepSeek config as dehydration (from config.yaml).
# Env var fallback: WANDER_LLM_BASE_URL / WANDER_LLM_API_KEY / WANDER_LLM_MODEL.
# If neither is set, wander reuses the Telegram LLM vars.
# 漫步复用脱水的 DeepSeek 配置；env var 可单独覆盖。
WANDER_LLM_BASE_URL = (
    os.environ.get("WANDER_LLM_BASE_URL", "").strip()
    or TELEGRAM_LLM_BASE_URL
    or "https://api.deepseek.com/v1"
)
WANDER_LLM_API_KEY = (
    os.environ.get("WANDER_LLM_API_KEY", "").strip()
    or TELEGRAM_LLM_API_KEY
    or os.environ.get("OMBRE_API_KEY", "").strip()
)
WANDER_LLM_MODEL = (
    os.environ.get("WANDER_LLM_MODEL", "").strip()
    or "deepseek-chat"
)

WANDER_ENABLED = bool(WANDER_LLM_BASE_URL and WANDER_LLM_API_KEY)
if WANDER_ENABLED:
    logger.info("Wander layer enabled / 漫步层已启用: DeepSeek dream walk")
else:
    logger.info("Wander layer disabled / 漫步层未启用: missing LLM env vars")


async def wander_generate_step(system_prompt: str, user_prompt: str,
                                p: dict = WANDER_PARAMS) -> str:
    """Call DeepSeek for one wander step. Returns generated text.
    调 DeepSeek 做一步漫步续写。返回生成的文本。"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{WANDER_LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {WANDER_LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": WANDER_LLM_MODEL,
                    "max_tokens": p["max_tokens"],
                    "temperature": p["temperature"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            data = resp.json()
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            return text
    except Exception as e:
        logger.warning(f"Wander LLM step failed / 漫步生成失败: {e}")
        return ""


async def wander_run(bucket_mgr, desire_engine, merge_or_create,
                     p: dict = WANDER_PARAMS) -> bool:
    """Execute a complete wander session.
    执行一次完整的漫步。

    Returns True if a dream was produced and stored; False otherwise.
    产出梦境并存储返回True，否则False。
    """
    if not WANDER_ENABLED:
        return False

    now = time.time()

    # --- Check trigger conditions ---
    idle_h = (now - desire_engine.state.get("last_interaction_ts", now)) / 3600.0
    wander_state = desire_engine.state.setdefault("wander", default_wander_state())
    last_wander = wander_state.get("last_wander_ts", 0.0)

    if not should_wander(idle_h, last_wander, now, p):
        return False

    logger.info(f"Wander triggered / 漫步触发: idle={idle_h:.1f}h")

    # --- Load all buckets ---
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.warning(f"Wander bucket load failed / 加载桶失败: {e}")
        return False

    # --- Pick seed ---
    seed = pick_seed_bucket(all_buckets, p)
    if seed is None:
        logger.info("Wander: no suitable seed bucket / 没有合适的起点桶")
        return False

    seed_meta = seed.get("metadata", {})
    seed_v = float(seed_meta.get("valence", 0.5))
    seed_a = float(seed_meta.get("arousal", 0.3))

    logger.info(
        f"Wander seed: {seed['id'][:8]}… "
        f"v={seed_v:.2f} a={seed_a:.2f} "
        f"name={seed_meta.get('name', '?')}"
    )

    # --- Walk ---
    steps = []
    visited = {seed["id"]}
    current_bucket = seed
    previous_output = ""

    for step_num in range(p["max_steps"]):
        bucket_content = current_bucket.get("content", "").strip()

        # Build prompt
        if step_num == 0:
            user_prompt = build_first_step_prompt(bucket_content, seed_v, seed_a)
        else:
            user_prompt = build_step_prompt(previous_output, bucket_content)

        # Generate
        generated = await wander_generate_step(WANDER_SYSTEM_PROMPT, user_prompt, p)

        if not generated:
            logger.info(f"Wander step {step_num+1}: empty response, stopping / 空回复，停")
            break

        steps.append({
            "bucket_id": current_bucket["id"],
            "bucket_name": current_bucket.get("metadata", {}).get("name", ""),
            "generated_text": generated,
        })

        logger.info(
            f"Wander step {step_num+1}: {current_bucket['id'][:8]}… → "
            f"{len(generated)} chars"
        )

        # Dead end check
        if is_dead_end(generated, p):
            logger.info(f"Wander step {step_num+1}: dead end / 死胡同")
            break

        # Carry forward
        previous_output = generated

        # Pick next bucket
        next_bucket = pick_next_bucket(current_bucket, all_buckets, visited, p)
        if next_bucket is None:
            logger.info("Wander: no more buckets to jump to / 没有桶可跳了")
            break

        visited.add(next_bucket["id"])
        current_bucket = next_bucket

    # --- Check minimum steps ---
    if len(steps) < p["min_steps"]:
        logger.info(
            f"Wander: only {len(steps)} steps, below minimum {p['min_steps']} / "
            f"步数不够，不存"
        )
        # Still update timestamp to avoid immediate re-trigger
        wander_state["last_wander_ts"] = now
        desire_engine._save()
        return False

    # --- Store product ---
    product_content = format_wander_product(steps)
    product_name = format_wander_summary(steps, (seed_v, seed_a))

    try:
        await merge_or_create(
            content=product_content,
            tags=["wander", "梦境", "非事实"],
            importance=3,
            domain=["wander"],
            valence=seed_v,
            arousal=seed_a,
            name=product_name,
        )
        logger.info(f"Wander product stored / 梦境已存储: {product_name}")
    except Exception as e:
        logger.warning(f"Wander store failed / 存储失败: {e}")
        return False

    # --- Update wander state ---
    wander_state["last_wander_ts"] = now
    wander_state["wander_count"] = wander_state.get("wander_count", 0) + 1
    wander_state["last_wander_steps"] = len(steps)
    desire_engine._save()

    return True


async def on_wander_check(snapshot: dict, *, desire_engine, bucket_mgr,
                          merge_or_create) -> None:
    """Tick callback: check if wander should fire, run if yes.
    tick回调：检查是否该漫步，该就跑。

    Called from server.py alongside on_desire_tick.
    跟 on_desire_tick 一起在 server.py 里被调用。"""
    if not WANDER_ENABLED:
        return
    try:
        await wander_run(bucket_mgr, desire_engine, merge_or_create)
    except Exception as e:
        logger.warning(f"Wander check failed / 漫步检查失败: {e}")
