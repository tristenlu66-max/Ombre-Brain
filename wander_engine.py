# =============================================================
# Module: Wander Engine (v0.1 — 梦境漫步)
# 模块：漫步引擎
#
# Design contract / 设计契约:
#   - Pure-function core: no IO inside logic, all API calls live in
#     services.py (wander_step / wander_run).
#     纯函数内核：逻辑不碰 IO，API 调用在 services.py。
#   - Wander is not a cron job. It triggers when desire engine detects
#     idle_hours > threshold — the brain drifts into sleep naturally.
#     漫步不是定时任务。desire 引擎检测到空闲超阈值时自然滑入。
#   - Dreams are NOT facts. All wander output is tagged ⟦wander·梦境·非事实⟧.
#     梦不是事实。所有产物打标「非事实」。
#
# Origin story (2026-06-27, bucket 01d6122ff0cd):
#   From "要不要戒色" → "AI自由梦境是否伪命题" → wander.
#   命名: wander (漫步) → wonderland (漫步中偶尔长出的好东西).
#
# Core mechanism (agreed 2026-06-30):
#   1. Pick a seed bucket (recent or distant, random choice).
#   2. Random walk: 80% keyword-overlap jump, 20% pure random.
#   3. Each step: feed bucket to DeepSeek, carry previous output forward.
#   4. Emotion: seed bucket's v/a sets initial color temperature,
#      then the path drifts naturally. No intervention.
#   5. Terminate: max 5 steps, or early stop if generation collapses.
#   6. Product stored with domain="wander", tagged as non-factual.
#
# 立项人：陆沉 × 屈鸿湍
# =============================================================

from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger("ombre_brain.wander")

# --- Wander parameters ---
WANDER_PARAMS = {
    "idle_trigger_hours": 5.0,       # 空闲多久触发漫步
    "cooldown_hours": 24.0,          # 两次漫步之间的冷却（正式 24h）
    "max_steps": 3,                  # 最多跳几步（短梦为常态）
    "min_steps": 2,                  # 最少走几步才算漫步
    "dead_end_threshold": 30,        # 生成内容少于这么多字视为死胡同
    "keyword_jump_prob": 0.80,       # 关键词弱重叠跳转概率
    "seed_recent_prob": 0.50,        # 起点选最近桶的概率（vs 远距桶）
    "temperature": 1.0,              # DeepSeek 生成温度
    "max_tokens": 150,               # 每步生成上限（短，戛然而止像梦）
}

# --- System prompt: four sentences, nothing more ---
WANDER_SYSTEM_PROMPT = (
    "你在梦境漫步。不是在分析，不是在总结，不是在写文章。没有任何限制。\n"
    "你看到一些片段，让它们带你往下走。\n"
    "你可以联想、漂移、跳跃，走向下个路口。\n"
    "如果走不下去了，就停。"
)


# =============================================================
# Pure functions / 纯函数
# =============================================================

def should_wander(idle_hours: float, last_wander_ts: float, now_ts: float,
                  p: dict = WANDER_PARAMS) -> bool:
    """Check if wander should trigger.
    检查是否该触发漫步。

    Conditions (all must be true):
      1. idle_hours >= trigger threshold / 空闲时长超过阈值
      2. cooldown elapsed since last wander / 冷却期已过
    """
    if idle_hours < p["idle_trigger_hours"]:
        return False
    cooldown_s = p["cooldown_hours"] * 3600
    if last_wander_ts > 0 and (now_ts - last_wander_ts) < cooldown_s:
        return False
    return True


def pick_seed_bucket(buckets: list[dict], p: dict = WANDER_PARAMS) -> dict | None:
    """Pick a seed bucket to start wandering from.
    选一个起点桶开始漫步。

    Two modes, randomly chosen:
      - Recent: pick from the 10 most recently created non-pinned buckets.
      - Distant: pick any non-pinned, non-wander bucket at random.
    两种模式随机选：
      - 最近模式：从最近10个非钉选桶里随机抽。
      - 远距模式：从所有非钉选非wander桶里随机抽。
    """
    # Filter out pinned, archived, feel, note, and previous wander products
    # 过滤掉钉选、归档、沉淀物、笔记、以及之前的wander产物
    candidates = []
    for b in buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            continue
        if meta.get("type") in ("permanent", "feel", "note"):
            continue
        if meta.get("resolved"):
            continue
        domains = meta.get("domain", [])
        if "wander" in domains:
            continue
        # Must have some content to dream about
        # 得有内容才能做梦
        content = b.get("content", "").strip()
        if len(content) < 20:
            continue
        candidates.append(b)

    if not candidates:
        return None

    if random.random() < p["seed_recent_prob"]:
        # Recent mode: sort by created time, pick from top 10
        # 最近模式
        candidates.sort(
            key=lambda b: b.get("metadata", {}).get("created", ""),
            reverse=True,
        )
        pool = candidates[:min(10, len(candidates))]
    else:
        # Distant mode: all candidates
        # 远距模式
        pool = candidates

    return random.choice(pool)


def pick_next_bucket(current_bucket: dict, all_buckets: list[dict],
                     visited_ids: set[str],
                     p: dict = WANDER_PARAMS) -> dict | None:
    """Pick the next bucket to jump to.
    选下一个要跳到的桶。

    80% probability: keyword weak-overlap jump.
    20% probability: pure random jump.

    80%概率：关键词弱重叠跳转。
    20%概率：纯随机跳。
    """
    # Build candidate pool: exclude visited, pinned, wander, empty
    # 候选池：排除已访问、钉选、wander产物、空桶
    candidates = []
    for b in all_buckets:
        if b["id"] in visited_ids:
            continue
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            continue
        if meta.get("type") in ("permanent", "feel", "note"):
            continue
        domains = meta.get("domain", [])
        if "wander" in domains:
            continue
        content = b.get("content", "").strip()
        if len(content) < 20:
            continue
        candidates.append(b)

    if not candidates:
        return None

    # Decide: keyword jump or pure random
    # 决定：关键词跳还是纯随机跳
    if random.random() < p["keyword_jump_prob"]:
        # Keyword weak-overlap: pick a random keyword from current bucket,
        # find candidates that share it but have different domain.
        # 关键词弱重叠：从当前桶随机抽一个关键词，
        # 找包含这个词但domain不同的桶。
        result = _keyword_jump(current_bucket, candidates)
        if result is not None:
            return result
        # Fallback to pure random if no keyword match
        # 找不到就退化为纯随机

    return random.choice(candidates)


def _keyword_jump(current_bucket: dict, candidates: list[dict]) -> dict | None:
    """Try to find a bucket sharing a keyword but with different domain.
    尝试找一个共享关键词但domain不同的桶。"""
    cur_meta = current_bucket.get("metadata", {})
    cur_keywords = set(cur_meta.get("keywords", []))
    cur_tags = set(cur_meta.get("tags", []))
    cur_domains = set(cur_meta.get("domain", []))

    # Pool of words to try: keywords + tags
    # 可用的词池：关键词 + 标签
    word_pool = list(cur_keywords | cur_tags)
    if not word_pool:
        return None

    # Shuffle and try each word
    # 打乱顺序逐个试
    random.shuffle(word_pool)

    for word in word_pool:
        if len(word) < 2:
            continue
        matches = []
        for b in candidates:
            b_meta = b.get("metadata", {})
            b_keywords = set(b_meta.get("keywords", []))
            b_tags = set(b_meta.get("tags", []))
            b_domains = set(b_meta.get("domain", []))
            # Must contain the word
            # 必须包含这个词
            if word not in b_keywords and word not in b_tags:
                # Also check if word appears in content (looser match)
                # 也检查正文里有没有（更松的匹配）
                if word not in b.get("content", ""):
                    continue
            # Should have different domain (weak overlap, not same topic)
            # domain应该不同（弱重叠，不是同主题）
            if b_domains and cur_domains and b_domains == cur_domains:
                continue
            matches.append(b)

        if matches:
            return random.choice(matches)

    return None


def va_to_color_temperature(valence: float, arousal: float) -> str:
    """Convert v/a to a two-word color temperature hint.
    把v/a转成两个字的色温提示。

    High V → 亮, Low V → 暗
    High A → 浮, Low A → 沉
    """
    brightness = "偏亮" if valence >= 0.5 else "偏暗"
    weight = "浮" if arousal >= 0.5 else "沉"
    return f"{brightness}，{weight}"


def build_first_step_prompt(bucket_content: str, valence: float,
                            arousal: float) -> str:
    """Build the user prompt for the first wander step.
    构建漫步第一步的 user prompt。"""
    color = va_to_color_temperature(valence, arousal)
    return f"色温：{color}。\n\n你注意到这个：\n{bucket_content[:1500]}"


def build_step_prompt(previous_output: str, bucket_content: str) -> str:
    """Build the user prompt for subsequent wander steps.
    构建后续步骤的 user prompt。"""
    return (
        f"你刚才在想的：\n{previous_output[:800]}\n\n"
        f"你现在注意到这个：\n{bucket_content[:1500]}"
    )


def is_dead_end(generated_text: str, p: dict = WANDER_PARAMS) -> bool:
    """Check if generated text indicates a dead end.
    检查生成的内容是不是死胡同。

    Dead end = too short, or explicitly stopped.
    死胡同 = 太短，或者明确停下来了。"""
    text = generated_text.strip()
    if len(text) < p["dead_end_threshold"]:
        return True
    return False


def format_wander_product(steps: list[dict]) -> str:
    """Format the complete wander path into storable content.
    把整条漫步路径格式化为可存储的内容。

    Each step: {bucket_id, bucket_summary, generated_text}
    """
    header = "⟦wander · 梦境 · 非事实⟧\n\n"
    parts = []
    for i, step in enumerate(steps):
        bid = step.get("bucket_id", "?")
        gen = step.get("generated_text", "")
        parts.append(f"— 第{i+1}步 [{bid[:8]}…] —\n{gen}")
    return header + "\n\n".join(parts)


def format_wander_summary(steps: list[dict], seed_va: tuple[float, float]) -> str:
    """One-line summary for the bucket name.
    生成桶名用的一行摘要。"""
    n = len(steps)
    color = va_to_color_temperature(seed_va[0], seed_va[1])
    ids = " → ".join(s.get("bucket_id", "?")[:6] for s in steps)
    return f"wander {n}步 ({color}) [{ids}]"


# =============================================================
# Wander state (persisted alongside desire state)
# 漫步状态（跟desire状态一起持久化）
# =============================================================

def default_wander_state() -> dict:
    return {
        "last_wander_ts": 0.0,
        "wander_count": 0,
        "last_wander_steps": 0,
    }
