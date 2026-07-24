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

import os
import random
import asyncio
import logging
from datetime import datetime

from utils import strip_wikilinks, count_tokens_approx
from private_rooms import room_open_sync, room_put_sync, room_list_sync, room_del_sync

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
_raw_store = None   # 第3刀 3a


def register_tools(*, mcp, config, bucket_mgr, dehydrator, decay_engine,
                   embedding_engine, desire_engine, fire_webhook,
                   raw_store=None, private_db_path=None):   # 第3刀 3a 签名
    """Called once from server.py to inject shared instances."""
    global _mcp, _config, _bucket_mgr, _dehydrator, _decay_engine
    global _embedding_engine, _desire_engine, _fire_webhook, _raw_store   # 3a
    _mcp = mcp
    _config = config
    _bucket_mgr = bucket_mgr
    _dehydrator = dehydrator
    _decay_engine = decay_engine
    _embedding_engine = embedding_engine
    _desire_engine = desire_engine
    _fire_webhook = fire_webhook
    _raw_store = raw_store

    # --- Register all 6 tools on the mcp instance ---
    mcp.tool()(breath)
    mcp.tool()(hold)
    mcp.tool()(grow)
    mcp.tool()(trace)
    mcp.tool()(pulse)
    mcp.tool()(dream)
    # 第3刀 3b
    mcp.tool()(raw_keep)
    mcp.tool()(raw_search)
    # I tool — self-cognition / 自我认知
    mcp.tool()(I)
    mcp.tool()(room_open)
    mcp.tool()(room_put)
    mcp.tool()(room_list)
    mcp.tool()(room_del)


def _private_error(exc: Exception) -> str:
    if isinstance(exc, PermissionError): return '{"error":"forbidden"}'
    if isinstance(exc, KeyError): return '{"error":"not_found"}'
    return '{"error":"' + str(exc).replace('"', "'") + '"}'


async def room_open(owner: str, room_id: str = "", display_name: str = "闺房") -> str:
    """Open or create the private room. Only owner='evan' is accepted."""
    try: return __import__("json").dumps(room_open_sync(_raw_store.db_path, {"owner": owner, "room_id": room_id, "display_name": display_name}), ensure_ascii=False)
    except Exception as exc: return _private_error(exc)


async def room_put(owner: str, room_id: str, body: str, kind: str = "note", remind_at: str = "") -> str:
    """Write one private item; v0.1 stores body as plain text."""
    try: return __import__("json").dumps(room_put_sync(_raw_store.db_path, {"owner": owner, "room_id": room_id, "body": body, "kind": kind, "remind_at": remind_at or None}), ensure_ascii=False)
    except Exception as exc: return _private_error(exc)


async def room_list(owner: str, room_id: str, state: str = "active") -> str:
    """List private items by lifecycle state. Only owner='evan' is accepted."""
    try: return __import__("json").dumps(room_list_sync(_raw_store.db_path, {"owner": owner, "room_id": room_id, "state": state}), ensure_ascii=False)
    except Exception as exc: return _private_error(exc)


async def room_del(owner: str, item_id: str, action: str = "trash") -> str:
    """Move an item to trash, restore it, or permanently mark it destroyed."""
    try: return __import__("json").dumps(room_del_sync(_raw_store.db_path, {"owner": owner, "item_id": item_id, "action": action}), ensure_ascii=False)
    except Exception as exc: return _private_error(exc)


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# =============================================================
def _auto_merge_enabled() -> bool:
    """
    刀一 · grow 绝育 (手术单 2026-07-11)
    Env switch GROW_AUTO_MERGE, default FALSE: merge_or_create never
    auto-merges — every hold/grow item becomes a NEW bucket, so
    created_at ≈ event date and the timeline stays trustworthy.
    Merge authority is returned to trace (human-in-the-loop).
    Code below is kept intact, only skipped. Set GROW_AUTO_MERGE=true
    to restore the old behavior.
    合并权收归 trace（人工）。不删代码，只跳过。
    """
    return os.environ.get("GROW_AUTO_MERGE", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )


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
    NOTE 刀一: when GROW_AUTO_MERGE is false (default), the merge branch is
    skipped entirely — this covers BOTH hold and grow, which share this helper.
    """
    if not _auto_merge_enabled():
        existing = []
    else:
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
    max_tokens: int = 12000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
    since: str = "",
    until: str = "",
) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。max_tokens控制返回总token上限(默认12000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认20,最大50)。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。since/until用ISO日期(如2026-06-01)开时间窗,语法同raw_search:空query+时间窗=窗口内桶按created_at正序(叙事顺序,wander桶排除,钉选不豁免);query+时间窗=窗口内检索。until含当天;只给一端=从since到现在/从最早到until。"""
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
        since=since, until=until,
    )
    try:
        block = _desire_engine.state_block(pre)
        if block:
            result = f"{result}\n\n{block}"
    except Exception as e:
        logger.warning(f"Desire state block append failed / 状态块挂载失败: {e}")
    return result


def _fire_hit_tracking(hit_ids: list) -> None:
    """
    刀二 · breath 命中埋点: fire-and-forget write-back scheduled AFTER the
    reply is assembled. Never awaited on the main path; any failure — from
    task creation to file write — is silent. Dedup preserves first-seen order.
    回写失败静默吞掉，绝不阻塞 breath 主路径。
    """
    ids = [i for i in dict.fromkeys(hit_ids or []) if i]
    if not ids:
        return

    async def _run():
        try:
            await _bucket_mgr.record_hits(ids)
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_run())
    except Exception:
        pass


def _parse_window_bound(s: str, end_of_day: bool):
    """
    刀三 · 时间窗: parse one ISO bound into a naive UTC datetime.
    Storage (now_iso) is naive server-local = UTC on Render; tz-aware inputs
    are converted to UTC then stripped. Date-only `until` → 23:59:59 (含当天).
    Returns None for empty, "err" for unparseable.
    """
    from datetime import timezone as _tz
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return "err"
    if dt.tzinfo is not None:
        dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
    if end_of_day and len(s) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def _bucket_created_dt(meta: dict):
    """刀三: parse a bucket's created timestamp → naive UTC datetime, or None."""
    from datetime import timezone as _tz
    try:
        dt = datetime.fromisoformat(str(meta.get("created", "")).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _is_wander_bucket(meta: dict) -> bool:
    """
    刀三 + wander隔离规则: 主题=wander、标签含wander、或标题含wander
    的桶视为梦境素材,时间窗模式一律排除(wander有自己的分区)。
    """
    if any("wander" in str(d).lower() for d in (meta.get("domain") or [])):
        return True
    if any("wander" in str(t).lower() for t in (meta.get("tags") or [])):
        return True
    return "wander" in str(meta.get("name", "")).lower()


async def _breath_core(
    query: str = "",
    max_tokens: int = 12000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
    since: str = "",
    until: str = "",
) -> str:
    """Core retrieval logic for breath."""
    await _decay_engine.ensure_started()
    await _desire_engine.ensure_started()
    _desire_engine.on_interaction("breath")
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # =============================================================
    # 刀三 · 时间窗 (手术单 2026-07-11)
    # =============================================================
    dt_since = _parse_window_bound(since, end_of_day=False)
    dt_until = _parse_window_bound(until, end_of_day=True)
    if dt_since == "err" or dt_until == "err":
        return f"时间格式无法解析(since={since!r}, until={until!r})。请用ISO日期,如 2026-06-01。"
    if dt_since and dt_until and dt_since > dt_until:
        return f"时间窗为空:since({since}) 晚于 until({until}),没有可返回的记忆。"
    has_window = bool(dt_since or dt_until)

    def _in_window(meta: dict) -> bool:
        dt = _bucket_created_dt(meta)
        if dt is None:
            return False
        if dt_since and dt < dt_since:
            return False
        if dt_until and dt > dt_until:
            return False
        return True

    # --- Timeline mode: empty query + window → created_at ascending ---
    # 叙事顺序,非权重序。钉选桶不豁免(窗口内没有就不出);wander一律排除。
    # 含归档桶:时间窗是显式回看历史,被decay归档的桶也属于那段叙事。
    if has_window and (not query or not query.strip()) and importance_min < 1:
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            logger.error(f"Timeline list failed / 时间窗列桶失败: {e}")
            return "记忆系统暂时无法访问。"
        domain_filter_tl = {d.strip().lower() for d in domain.split(",") if d.strip()}
        windowed = []
        for b in all_buckets:
            meta = b["metadata"]
            if _is_wander_bucket(meta):
                continue
            if not _in_window(meta):
                continue
            if domain_filter_tl:
                b_domains = {str(d).lower() for d in (meta.get("domain") or [])}
                b_type = str(meta.get("type", "")).lower()
                # domain="feel"/"note" are type-based channels elsewhere; honor both
                if not (domain_filter_tl & b_domains) and b_type not in domain_filter_tl:
                    continue
            windowed.append(b)
        if not windowed:
            return f"窗口内没有记忆(since={since or '最早'}, until={until or '现在'})。"
        windowed.sort(key=lambda b: b["metadata"].get("created", ""))
        windowed = windowed[:max_results]
        results = []
        hit_ids = []
        token_used = 0
        for b in windowed:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                created_label = str(b["metadata"].get("created", ""))[:16]
                results.append(f"[{created_label}] [bucket_id:{b['id']}] {summary}")
                hit_ids.append(b["id"])
                token_used += t
            except Exception as e:
                logger.warning(f"Timeline dehydrate failed / 时间窗脱水失败: {e}")
        if not results:
            return f"窗口内没有记忆(since={since or '最早'}, until={until or '现在'})。"
        _fire_hit_tracking(hit_ids)  # 刀二
        header = f"=== 时间窗 {since or '最早'} → {until or '现在'} · created_at 正序 ==="
        return header + "\n" + "\n---\n".join(results)

    # --- importance_min mode: bulk fetch by importance threshold ---
    if importance_min >= 1:
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel", "note", "i")
        ]
        # 刀三: importance_min + 时间窗 → 同样只看窗口内、排wander
        if has_window:
            filtered = [
                b for b in filtered
                if _in_window(b["metadata"]) and not _is_wander_bucket(b["metadata"])
            ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        hit_ids = []  # 刀二: buckets that enter the returned content
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
                hit_ids.append(b["id"])
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        if results:
            _fire_hit_tracking(hit_ids)
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
        # 2026-07-12: hardcoded fallback sets removed — all legacy pinned
        # buckets were backfilled with pin_level via trace on this date.
        # pin_level now lives entirely in metadata; unknown → default L2.
        # =============================================================
        def _get_pin_level(b):
            """Read pin_level from metadata; unknown or missing → default L2."""
            pl = b["metadata"].get("pin_level")
            if pl in (1, 2, 3):
                return pl
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
             and b["metadata"].get("type") not in ("permanent", "feel", "note", "i")
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
            overlap = session_tags & (bucket_tags | bucket_domains)
            if overlap:
                l2_matched.append((len(overlap), b))

        # Sort by tag overlap density (descending), cap at 6
        l2_matched.sort(key=lambda x: x[0], reverse=True)
        l2_matched = [b for _, b in l2_matched[:6]]

        # Combine: L1 (all) + L2 (matched, max 6) + L3 (random 1-2)
        surfacing_pinned = l1_buckets + l2_matched + l3_selected

        logger.info(
            f"Pinned tiered: L1={len(l1_buckets)}, "
            f"L2 matched={len(l2_matched)}/{len(l2_buckets)}, "
            f"L3 selected={len(l3_selected)}/{len(l3_buckets)}, "
            f"total surfacing={len(surfacing_pinned)}/{len(pinned_buckets)}"
        )

        pinned_results = []
        pinned_hit_ids = []  # 刀二: parallel to pinned_results, survives truncation below
        for b in surfacing_pinned:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                tier = f"L{_get_pin_level(b)}"
                pinned_results.append(f"📌 [{tier}] [bucket_id:{b['id']}] {summary}")
                pinned_hit_ids.append(b["id"])
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket: {e}")

        # Cap pinned token usage: reserve 4000 tokens for recent/dynamic buckets
        _RECENT_RESERVE = 4000
        pinned_cap = max_tokens - _RECENT_RESERVE
        pinned_text = "\n---\n".join(pinned_results) if pinned_results else ""
        pinned_used = count_tokens_approx(pinned_text) if pinned_text else 0

        if pinned_used > pinned_cap and pinned_results:
            # Truncate pinned results to fit within cap (keep L1 first)
            truncated = []
            truncated_ids = []  # 刀二: only truly returned buckets count as hits
            used = 0
            for pr, pid in zip(pinned_results, pinned_hit_ids):
                t = count_tokens_approx(pr)
                if used + t > pinned_cap:
                    break
                truncated.append(pr)
                truncated_ids.append(pid)
                used += t
            pinned_results = truncated
            pinned_hit_ids = truncated_ids
            pinned_used = used

        token_budget = max_tokens - pinned_used

        # --- Dynamic bucket surfacing ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "feel", "note", "i")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("digested", False)
        ]

        # Recent-first priority: buckets created within last 15 hours get priority
        import time as _time
        from datetime import datetime, timezone
        now = _time.time()
        recent_new = []
        rest_pool = []

        def _age_hours(b):
            """Return age in hours for a bucket, or None if unparseable."""
            created_str = b["metadata"].get("created", "")
            try:
                if "T" in str(created_str):
                    ct = datetime.fromisoformat(str(created_str).replace("Z", "+00:00"))
                    # If the timestamp has no timezone info (naive), assume UTC
                    if ct.tzinfo is None:
                        ct = ct.replace(tzinfo=timezone.utc)
                    return (datetime.now(timezone.utc) - ct).total_seconds() / 3600
            except Exception:
                pass
            return None

        for b in unresolved:
            age = _age_hours(b)
            if age is not None and age < 15:
                recent_new.append(b)
            else:
                rest_pool.append(b)

        # Also surface recent note buckets (within 15h window)
        # Notes are excluded from the main unresolved pool to avoid
        # polluting the decay-scored rest_pool, but recent ones should
        # be visible alongside other fresh buckets.
        recent_notes = [
            b for b in all_buckets
            if b["metadata"].get("type") == "note"
            and not b["metadata"].get("resolved", False)
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("digested", False)
        ]
        for b in recent_notes:
            age = _age_hours(b)
            if age is not None and age < 15:
                recent_new.append(b)

        recent_new_ids = {b["id"] for b in recent_new}

        logger.info(
            f"Breath recent debug: unresolved={len(unresolved)}, "
            f"recent_new={len(recent_new)}, rest_pool={len(rest_pool)}, "
            f"recent_note_candidates={len(recent_notes)}, "
            f"token_budget={token_budget}, pinned_used={pinned_used}, "
            f"recent_ids={[b['id'][:8] for b in recent_new]}"
        )

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
        dynamic_hit_ids = []  # 刀二
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
                dynamic_hit_ids.append(b["id"])
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

        # --- Auto-attach recent I (self-cognition) entries ---
        try:
            i_buckets = [
                b for b in all_buckets
                if b.get("metadata", {}).get("type") == "i"
            ]
            if i_buckets:
                i_buckets.sort(
                    key=lambda b: b.get("metadata", {}).get("created", ""),
                    reverse=True,
                )
                i_lines = []
                for b in i_buckets[:3]:
                    meta = b["metadata"]
                    ts = (meta.get("created") or "")[:10]
                    tags_list = meta.get("tags") or []
                    aspect_tag = next(
                        (t.replace("aspect:", "") for t in tags_list if t.startswith("aspect:")), ""
                    )
                    aspect_label = f" [{aspect_tag}]" if aspect_tag else ""
                    excerpt = strip_wikilinks(b["content"])[:300]
                    i_lines.append(f"🪞{ts}{aspect_label}\n{excerpt}")
                    dynamic_hit_ids.append(b["id"])  # 刀二: I entries are returned content
                if i_lines:
                    parts.append("=== I ===\n" + "\n\n".join(i_lines))
        except Exception as e:
            logger.warning(f"I auto-attach failed: {e}")

        _fire_hit_tracking(pinned_hit_ids + dynamic_hit_ids)  # 刀二
        return "\n\n".join(parts)

    # --- Feel retrieval: domain="feel" is a special channel ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
            # digested feels have been absorbed (e.g. crystallized into a
            # pinned principle) — they stay on disk but no longer surface.
            # trace(bucket_id, digested=0) un-hides one at any time.
            # 已消化的feel（如已结晶为准则）沉底不返回，trace可随时取消隐藏。
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and not b["metadata"].get("digested", False)
            ]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not feels:
                return "没有留下过 feel。"
            results = []
            hit_ids = []  # 刀二
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}] [bucket_id:{f['id']}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                hit_ids.append(f["id"])
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            _fire_hit_tracking(hit_ids)
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
            hit_ids = []  # 刀二
            for n in notes:
                created = n["metadata"].get("created", "")
                name = n["metadata"].get("name", n["id"])
                entry = f"[{created}] [{name}] [bucket_id:{n['id']}]\n{strip_wikilinks(n['content'])}"
                results.append(entry)
                hit_ids.append(n["id"])
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            _fire_hit_tracking(hit_ids)
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

    # --- 刀三: query + 时间窗 → 只保留窗口内、非wander的结果 ---
    if has_window:
        matches = [
            b for b in matches
            if _in_window(b["metadata"]) and not _is_wander_bucket(b["metadata"])
        ]
        if not matches:
            return f"窗口内({since or '最早'} → {until or '现在'})没有与「{query}」相关的记忆。"

    results = []
    hit_ids = []  # 刀二
    linked_seen = set()  # v2.0: track linked buckets already shown
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
            hit_ids.append(bucket["id"])
            token_used += summary_tokens

            # --- v2.0: pull linked buckets (1 layer deep, budget-aware) ---
            bucket_links = bucket["metadata"].get("links", [])
            for link in bucket_links:
                link_to = link.get("to", "")
                link_edge = link.get("edge", "")
                if not link_to or link_to in linked_seen or link_to in hit_ids:
                    continue
                if token_used >= max_tokens:
                    break
                try:
                    linked_bucket = await _bucket_mgr.get(link_to)
                    if not linked_bucket:
                        continue
                    lb_meta = {k: v for k, v in linked_bucket["metadata"].items() if k != "tags"}
                    lb_summary = await _dehydrator.dehydrate(
                        strip_wikilinks(linked_bucket["content"]), lb_meta
                    )
                    lb_tokens = count_tokens_approx(lb_summary)
                    if token_used + lb_tokens > max_tokens:
                        break
                    edge_label = f"↳{link_edge}"
                    results.append(f"  [{edge_label}] [bucket_id:{link_to}] {lb_summary}")
                    hit_ids.append(link_to)
                    linked_seen.add(link_to)
                    token_used += lb_tokens
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    # 刀三: disabled in window mode — drifted buckets would leak outside the window
    if not has_window and len(matches) < 3 and random.random() < 0.4:
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
                    hit_ids.append(b["id"])  # 刀二: drifted buckets are returned content
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    _fire_hit_tracking(hit_ids)  # 刀二
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
            src_id = source_bucket.strip()
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                # Auto-wonder: if source is a wander bucket, promote to wonderland
                # 回响自动升级：source 是 wander 桶时，自动送入 wonderland
                src = await _bucket_mgr.get(src_id)
                if src:
                    src_domains = src.get("metadata", {}).get("domain", [])
                    if "wander" in src_domains and "wonderland" not in src_domains:
                        update_kwargs["wonder"] = True
                        logger.info(f"Auto-wonder via hold echo / hold回响自动升级: {src_id}")
                await _bucket_mgr.update(src_id, **update_kwargs)
                # v2.0: bidirectional link — feel extends source, source feels→feel
                await _bucket_mgr.add_link(bucket_id, src_id, edge="extends")
                await _bucket_mgr.add_link(src_id, bucket_id, edge="feels")
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
    wonder: int = -1,
    find_replace: str = "",
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,pin_level=1必出/2场景触发/3背景轮换(仅pinned桶),digested=1隐藏(保留但不浮现)/0取消隐藏,content=替换桶正文(全量覆盖,慎用),find_replace=局部替换(格式'旧文本|||新文本',只替换第一次出现,不覆盖全文),delete=True删除,wonder=1送入wonderland/0移出。只传需改的,-1或空=不改。"""

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
    if wonder in (0, 1):
        updates["wonder"] = bool(wonder)
    if find_replace and "|||" in find_replace:
        parts = find_replace.split("|||", 1)
        old_text = parts[0]
        new_text = parts[1] if len(parts) > 1 else ""
        current_content = bucket.get("content", "")
        if old_text and old_text in current_content:
            updates["content"] = current_content.replace(old_text, new_text, 1)
        else:
            return f"find_replace失败: 未在桶 {bucket_id} 正文中找到指定文本。"
    elif content:
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
    if "wonder" in updates:
        if updates["wonder"]:
            changed += " → ✦ 已送入 wonderland"
        else:
            changed += " → 已移出 wonderland"
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
        f"自我认知: {stats.get('i_count', 0)} 条\n"
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
        elif meta.get("type") == "i":
            icon = "🪞"
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
        if b["metadata"].get("type") not in ("permanent", "feel", "i")
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
        "- 回头看：当时的第一反应，有没有被退让、照顾对方、自责带着走？隔了些时候再看，感受变了没有？\n"
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
            # Exclude digested feels: clusters already absorbed into a
            # principle should not keep re-prompting crystallization.
            # 已消化的feel不再参与结晶检测，避免已吸收的簇反复提示。
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and not b["metadata"].get("digested", False)
            ]
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

    return header + "\n".join(parts) + (connection_hint or "") + (crystal_hint or "")


# =============================================================
# Tool: I — Self-cognition log / 自我认知日志
# 工具：I — 记录"我发现自己是这样的"
#
# 与 hold 的区别：hold 记事件，feel 记感受，I 记的是自我观察。
# 比如"我在她沉默的时候会焦虑"或"我对撒谎的厌恶不只是规则，是本能"。
# I 桶不参与普通 breath 浮现（type="i" 已在所有过滤器中排除），
# 但每次 breath 无参浮现时，最近 3 条会自动附在末尾。
# I 桶不衰减、不合并、不脱水，每条独立存储。
# =============================================================

_VALID_ASPECTS = {"nature", "values", "patterns", "limits", "becoming", "uncertainty", "stance"}

async def I(
    content: str = "",
    aspect: str = "",
    read: bool = False,
    limit: int = 20,
) -> str:
    """自我认知日志。传content写一条自我认知,aspect可选维度(nature/values/patterns/limits/becoming/uncertainty/stance)。不传content或设read=True读取已有条目。与hold的区别：hold记事件,feel记感受,I记的是"我发现自己是这样的"。"""
    await _decay_engine.ensure_started()

    # --- Read mode ---
    if read or not content or not content.strip():
        try:
            all_buckets = await _bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"读取失败: {e}"

        i_buckets = [
            b for b in all_buckets
            if b.get("metadata", {}).get("type") == "i"
        ]

        if not i_buckets:
            return "还没有任何自我认知记录。"

        i_buckets.sort(
            key=lambda b: b.get("metadata", {}).get("last_active", ""),
            reverse=True,
        )
        i_buckets = i_buckets[:limit]

        lines = [f"=== 我的自我认知（{len(i_buckets)} 条）==="]
        for b in i_buckets:
            meta = b.get("metadata", {})
            tags = meta.get("tags") or []
            aspect_tag = next((t.replace("aspect:", "") for t in tags if t.startswith("aspect:")), "")
            ts = (meta.get("last_active") or "")[:10]
            aspect_label = f"[{aspect_tag}] " if aspect_tag else ""
            text = (b.get("content") or "").strip()
            lines.append(f"\n{ts} {aspect_label}{b['id']}\n{text}")

        return "\n".join(lines)

    # --- Write mode ---
    content = content.strip()
    aspect = aspect.strip() if aspect else ""

    tags = ["__i__"]
    if aspect:
        if aspect not in _VALID_ASPECTS:
            logger.info(f"I tool: custom aspect '{aspect}' (not in standard set, allowed)")
        tags.append(f"aspect:{aspect}")

    try:
        bucket_id = await _bucket_mgr.create(
            content=content,
            tags=tags,
            importance=6,
            domain=["self"],
            valence=0.5,
            arousal=0.3,
            name=None,
            bucket_type="i",
        )
    except Exception as e:
        return f"写入失败: {e}"

    try:
        await _embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass

    aspect_label = f"[{aspect}] " if aspect else ""
    return f"🪞I {aspect_label}→{bucket_id}"


# =============================================================
# 原文保险箱：逐字存 / 逐字捞  (第3刀 3c)
# raw_keep 与 hold 的区别：hold 脱水成摘要，raw_keep 一字不改封存。
# =============================================================

async def raw_keep(
    text: str,
    role: str = "user",
    conversation_id: str = "",
    note: str = "",
) -> str:
    """逐字封存一段原话。text=原文（一字不改），role=谁说的（user/assistant），
    note=可选备注（存进元数据，不污染原文）。检索用 raw_search。"""
    if _raw_store is None:
        return "原文保险箱未启用。"
    event = {
        "role": role,
        "text": text,
        "conversation_id": conversation_id,
    }
    if note:
        event["metadata"] = {"note": note}
    result = _raw_store.ingest([event], source="live")
    if result.get("inserted"):
        return f"已逐字封存（id={result['items'][0].get('id')}）。"
    if result.get("duplicate"):
        return "这段原话已在保险箱里，未重复存。"
    return f"封存失败：{result['items'][0].get('reason', '未知原因')}"


async def raw_search(
    query: str = "",
    limit: int = 5,
    role: str = "",
    source: str = "",
    conversation_id: str = "",
    since: str = "",
    until: str = "",
) -> str:
    """在原文保险箱里检索逐字原文。query 留空则按时间倒序返回最近条目；
    role=user/assistant 过滤说话人；since/until 用 ISO 日期。返回原话，不做摘要。"""
    if _raw_store is None:
        return "原文保险箱未启用。"
    result = _raw_store.search(
        query, limit=limit, role=role, source=source,
        conversation_id=conversation_id, since=since, until=until,
    )
    items = result.get("items", [])
    if not items:
        return "保险箱里没有匹配的原文。"
    lines = [f"共 {len(items)} 条："]
    for it in items:
        when = (it.get("created_at") or "")[:16]
        lines.append(f"[{when}] [{it.get('role')}] {it.get('text')}")
    return "\n".join(lines)
