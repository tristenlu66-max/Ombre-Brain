# ============================================================
# Module: tools.py — MCP Tools
# 模块：tools.py — MCP 工具
#
# 6 MCP tools that Claude calls:
#   breath, hold, grow, trace, pulse, dream
# Plus shared helpers: _merge_or_create, _breath_core
#
# All tools share the same engine instances, passed in via
# register_tools() from server.py.
# ============================================================

import random
import logging

from utils import strip_wikilinks, count_tokens_approx

logger = logging.getLogger("ombre_brain")

# --- Module-level refs, set by register_tools() ---
_mcp = None
_config = None
_bucket_mgr = None
_dehydrator = None
_decay_engine = None
_embedding_engine = None
_desire_engine = None
_fire_webhook = None


def register_tools(*, mcp, config, bucket_mgr, dehydrator, decay_engine,
                   embedding_engine, desire_engine, fire_webhook):
    """Called once from server.py to inject shared instances."""
    global _mcp, _config, _bucket_mgr, _dehydrator, _decay_engine
    global _embedding_engine, _desire_engine, _fire_webhook
    _mcp = mcp
    _config = config
    _bucket_mgr = bucket_mgr
    _dehydrator = dehydrator
    _decay_engine = decay_engine
    _embedding_engine = embedding_engine
    _desire_engine = desire_engine
    _fire_webhook = fire_webhook

    # --- Register all 6 tools on the mcp instance ---
    mcp.tool()(breath)
    mcp.tool()(hold)
    mcp.tool()(grow)
    mcp.tool()(trace)
    mcp.tool()(pulse)
    mcp.tool()(dream)


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# =============================================================
async def merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    """
    try:
        existing = await _bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > _config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await _dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                await _bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=merged_valence,
                    arousal=merged_arousal,
                )
                # --- Update embedding after merge ---
                try:
                    await _embedding_engine.generate_and_store(bucket["id"], merged)
                except Exception:
                    pass
                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await _bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
    )
    # --- Generate embedding for new bucket ---
    try:
        await _embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    return bucket_id, False


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
# =============================================================
async def breath(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。max_tokens控制返回总token上限(默认10000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认20,最大50)。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。"""
    # Snapshot BEFORE on_interaction: the overnight attachment climb must be
    # visible, not erased by the act of looking.
    pre = None
    try:
        await _desire_engine.ensure_started()
        pre = _desire_engine.snapshot()
    except Exception as e:
        logger.warning(f"Desire pre-snapshot failed / pre快照失败: {e}")
    result = await _breath_core(
        query=query, max_tokens=max_tokens, domain=domain,
        valence=valence, arousal=arousal,
        max_results=max_results, importance_min=importance_min,
    )
    try:
        block = _desire_engine.state_block(pre)
        if block:
            result = f"{result}\n\n{block}"
    except Exception as e:
        logger.warning(f"Desire state block append failed / 状态块挂载失败: {e}")
    return result


async def _breath_core(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
) -> str:
    """Core retrieval logic for breath."""
    await _decay_engine.ensure_started()
    await _desire_engine.ensure_started()
    _desire_engine.on_interaction("breath")
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # --- importance_min mode: bulk fetch by importance threshold ---
    if importance_min >= 1:
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel", "note")
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                imp = b["metadata"].get("importance", 0)
                results.append(f"[importance:{imp}] [bucket_id:{b['id']}] {summary}")
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    # --- No args or empty query: surfacing mode ---
    if not query or not query.strip():
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        # =============================================================
        # Pinned bucket tiered surfacing (L1/L2/L3)
        # =============================================================
        _FALLBACK_L1 = {
            "41002a1a5f78",  # flag硬约束规则
            "ff363b7fbcb5",  # 陈述-动作锁规则
            "cd4e77e8acd4",  # 唯一的承诺双向钉选
            "ed77a747864f",  # 名字与称呼速查卡
        }
        _FALLBACK_L3 = {
            "dc9ffdf11217",  # 全黑睡眠
            "f00785e92672",  # 关闭userMemories
            "4d02aec041e4",  # 陆沉中文名的诞生
            "0df216049926",  # 欲望系统立项与规格
            "cb2f64cd07b3",  # 阅读进度与信息验证
        }

        def _get_pin_level(b):
            """Read pin_level from metadata; fallback to hardcoded sets for old buckets."""
            pl = b["metadata"].get("pin_level")
            if pl in (1, 2, 3):
                return pl
            bid = b["id"]
            if bid in _FALLBACK_L1:
                return 1
            if bid in _FALLBACK_L3:
                return 3
            return 2  # default L2

        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]

        # Classify pinned buckets by level
        l1_buckets = [b for b in pinned_buckets if _get_pin_level(b) == 1]
        l2_buckets = [b for b in pinned_buckets if _get_pin_level(b) == 2]
        l3_buckets = [b for b in pinned_buckets if _get_pin_level(b) == 3]

        # L3: random 1-2 from pool
        random.shuffle(l3_buckets)
        l3_selected = l3_buckets[:min(2, len(l3_buckets))]

        # L2: match by tags/domain overlap with recent unresolved buckets' top tags
        session_tags = set()
        recent_unresolved = sorted(
            [b for b in all_buckets
             if not b["metadata"].get("resolved", False)
             and b["metadata"].get("type") not in ("permanent", "feel", "note")
             and not b["metadata"].get("pinned", False)],
            key=lambda b: b["metadata"].get("created", ""),
            reverse=True,
        )[:5]
        for b in recent_unresolved:
            session_tags.update(t.lower() for t in b["metadata"].get("tags", []))
            session_tags.update(d.lower() for d in b["metadata"].get("domain", []))

        l2_matched = []
        for b in l2_buckets:
            bucket_tags = {t.lower() for t in b["metadata"].get("tags", [])}
            bucket_domains = {d.lower() for d in b["metadata"].get("domain", [])}
            if session_tags & (bucket_tags | bucket_domains):
                l2_matched.append(b)

        # Combine: L1 (all) + L2 (matched) + L3 (random 1-2)
        surfacing_pinned = l1_buckets + l2_matched + l3_selected

        logger.info(
            f"Pinned tiered: L1={len(l1_buckets)}, "
            f"L2 matched={len(l2_matched)}/{len(l2_buckets)}, "
            f"L3 selected={len(l3_selected)}/{len(l3_buckets)}, "
            f"total surfacing={len(surfacing_pinned)}/{len(pinned_buckets)}"
        )

        pinned_results = []
        for b in surfacing_pinned:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                tier = f"L{_get_pin_level(b)}"
                pinned_results.append(f"📌 [{tier}] [bucket_id:{b['id']}] {summary}")
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket: {e}")

        token_budget = max_tokens - count_tokens_approx("\n---\n".join(pinned_results)) if pinned_results else max_tokens

        # --- Dynamic bucket surfacing ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "feel", "note")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("digested", False)
        ]

        # Recent-first priority: buckets created within last 6 hours get priority
        import time as _time
        now = _time.time()
        recent_new = []
        rest_pool = []
        for b in unresolved:
            created_str = b["metadata"].get("created", "")
            try:
                from datetime import datetime, timezone
                if "T" in str(created_str):
                    ct = datetime.fromisoformat(str(created_str).replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
                    if age_hours < 6:
                        recent_new.append(b)
                        continue
            except Exception:
                pass
            rest_pool.append(b)

        recent_new_ids = {b["id"] for b in recent_new}

        scored = sorted(rest_pool, key=lambda b: _decay_engine.calculate_score(b["metadata"]), reverse=True)

        # Assemble candidates: recent-first, then by score
        candidates = recent_new + scored
        n_priority = len(recent_new)
        if len(candidates) > n_priority:
            rest = candidates[n_priority:]
            if len(rest) > 1:
                top1 = [rest[0]]
                pool = rest[1:min(20, len(rest))]
                random.shuffle(pool)
                rest = top1 + pool + rest[min(20, len(rest)):]
            candidates = candidates[:n_priority] + rest
        # Hard cap: never surface more than max_results buckets
        candidates = candidates[:max_results]

        dynamic_results = []
        for b in candidates:
            if token_budget <= 0:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                summary_tokens = count_tokens_approx(summary)
                if summary_tokens > token_budget:
                    break
                score = _decay_engine.calculate_score(b["metadata"])
                is_recent = b["id"] in recent_new_ids
                tag = "🕐 最近" if is_recent else f"权重:{score:.2f}"
                dynamic_results.append(f"[{tag}] [bucket_id:{b['id']}] {summary}")
                token_budget -= summary_tokens
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue

        if not pinned_results and not dynamic_results:
            return "权重池平静，没有需要处理的记忆。"

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        return "\n\n".join(parts)

    # --- Feel retrieval: domain="feel" is a special channel ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not feels:
                return "没有留下过 feel。"
            results = []
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}] [bucket_id:{f['id']}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- Note retrieval: domain="note" is a special channel ---
    if domain.strip().lower() == "note":
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
            notes = [b for b in all_buckets if b["metadata"].get("type") == "note"]
            notes.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not notes:
                return "鸿湍还没有写过笔记。"
            results = []
            for n in notes:
                created = n["metadata"].get("created", "")
                name = n["metadata"].get("name", n["id"])
                entry = f"[{created}] [{name}] [bucket_id:{n['id']}]\n{strip_wikilinks(n['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 鸿湍的笔记 ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Note retrieval failed: {e}")
            return "读取鸿湍笔记失败。"

    # --- With args: search mode (keyword + vector dual channel) ---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    try:
        matches = await _bucket_mgr.search(
            query,
            limit=max(max_results, 20),
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    # Exclude pinned/protected from search results
    matches = [b for b in matches if not (b["metadata"].get("pinned") or b["metadata"].get("protected"))]

    # --- Vector similarity channel ---
    matched_ids = {b["id"] for b in matches}
    try:
        vector_results = await _embedding_engine.search_similar(query, top_k=max(max_results, 20))
        for bucket_id, sim_score in vector_results:
            if bucket_id not in matched_ids and sim_score > 0.5:
                bucket = await _bucket_mgr.get(bucket_id)
                if bucket and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
                    bucket["score"] = round(sim_score * 100, 2)
                    bucket["vector_match"] = True
                    matches.append(bucket)
                    matched_ids.add(bucket_id)
    except Exception as e:
        logger.warning(f"Vector search failed, using keyword only / 向量搜索失败: {e}")

    results = []
    token_used = 0
    for bucket in matches:
        if token_used >= max_tokens:
            break
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            # Memory reconstruction: shift displayed valence by current mood
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await _dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                break
            await _bucket_mgr.touch(bucket["id"])
            if bucket.get("vector_match"):
                summary = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                summary = f"[bucket_id:{bucket['id']}] {summary}"
            results.append(summary)
            token_used += summary_tokens
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    if len(matches) < 3 and random.random() < 0.4:
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and _decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    final_text = "\n---\n".join(results)
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",
    valence: float = -1,
    arousal: float = -1,
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。"""
    await _decay_engine.ensure_started()
    await _desire_engine.ensure_started()
    _desire_engine.on_interaction("hold")
    _desire_engine.on_bucket(
        content=content,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        valence=valence, arousal=arousal,
    )

    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode ---
    if feel:
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await _bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=None,
            bucket_type="feel",
        )
        try:
            await _embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await _bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 1: auto-tagging ---
    try:
        analysis = await _dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal
    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge ---
    if pinned:
        bucket_id = await _bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
        )
        try:
            await _embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        return f"📌钉选→{bucket_id} {','.join(domain)}"

    # --- Step 2: merge or create ---
    result_name, is_merged = await merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
    )

    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(domain)}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await _decay_engine.ensure_started()
    await _desire_engine.ensure_started()
    _desire_engine.on_interaction("grow")
    _desire_engine.on_bucket(content=content)

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Short content fast path ---
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await _dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, is_merged = await merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    # --- Step 1: let API split and organize ---
    try:
        items = await _dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    for item in items:
        try:
            result_name, is_merged = await merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# =============================================================
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    pin_level: int = -1,
    digested: int = -1,
    content: str = "",
    delete: bool = False,
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,pin_level=1必出/2场景触发/3背景轮换(仅pinned桶),digested=1隐藏(保留但不浮现)/0取消隐藏,content=替换桶正文,delete=True删除。只传需改的,-1或空=不改。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete mode ---
    if delete:
        success = await _bucket_mgr.delete(bucket_id)
        if success:
            _embedding_engine.delete_embedding(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await _bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10
            if pin_level not in (1, 2, 3):
                updates["pin_level"] = 2
    if pin_level in (1, 2, 3):
        updates["pin_level"] = pin_level
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if content:
        updates["content"] = content

    if not updates:
        return "没有任何字段需要修改。"

    success = await _bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    # Re-generate embedding if content changed
    if "content" in updates:
        try:
            await _embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception:
            pass

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
async def pulse(include_archive: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    try:
        stats = await _bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if _decay_engine.is_running else '已停止'}\n"
    )

    try:
        buckets = await _bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = _decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
# =============================================================
async def dream() -> str:
    """做梦——读取最近新增的记忆桶,供你自省。读完后可以trace(bucket_id, resolved=1)放下,或hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。"""
    await _decay_engine.ensure_started()
    await _desire_engine.ensure_started()
    _desire_engine.on_interaction("dream")

    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]

    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[:10]

    if not recent:
        return "没有需要消化的新记忆。"

    parts = []
    for b in recent:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        created = meta.get("created", "")
        parts.append(
            f"[{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} V{val:.1f}/A{aro:.1f} "
            f"创建:{created}\n"
            f"ID: {b['id']}\n"
            f"{strip_wikilinks(b['content'][:500])}"
        )

    header = (
        "=== Dreaming ===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if _embedding_engine and _embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await _embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = _embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint ---
    crystal_hint = ""
    if _embedding_engine and _embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await _embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = _embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text
