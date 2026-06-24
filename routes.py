# ============================================================
# Module: routes.py — HTTP Endpoints
# 模块：routes.py — HTTP 端点
#
# All @mcp.custom_route endpoints:
#   - Auth (status/setup/login/logout/change-password)
#   - Hooks (breath-hook/dream-hook/wakeup)
#   - Dashboard API (buckets/search/network/config/status...)
#   - Desire panel
#   - Import/Export/Restore
#   - Calendar/Timeline/Note
#   - Health/Root redirect
#
# All routes share engine instances via register_routes().
# ============================================================

import os
import random
import hashlib
import hmac
import secrets
import time
import logging
import asyncio
import json as _json_lib

from utils import strip_wikilinks, count_tokens_approx, sanitize_name
import frontmatter

logger = logging.getLogger("ombre_brain")

# --- Module-level refs, set by register_routes() ---
_mcp = None
_config = None
_bucket_mgr = None
_dehydrator = None
_decay_engine = None
_embedding_engine = None
_desire_engine = None
_import_engine = None
_fire_webhook = None

# --- Session store (lost on restart, 7-day expiry) ---
_sessions: dict[str, float] = {}


def register_routes(*, mcp, config, bucket_mgr, dehydrator, decay_engine,
                    embedding_engine, desire_engine, import_engine, fire_webhook):
    """Called once from server.py to inject shared instances and register all routes."""
    global _mcp, _config, _bucket_mgr, _dehydrator, _decay_engine
    global _embedding_engine, _desire_engine, _import_engine, _fire_webhook
    _mcp = mcp
    _config = config
    _bucket_mgr = bucket_mgr
    _dehydrator = dehydrator
    _decay_engine = decay_engine
    _embedding_engine = embedding_engine
    _desire_engine = desire_engine
    _import_engine = import_engine
    _fire_webhook = fire_webhook

    # --- Register all routes ---
    mcp.custom_route("/", methods=["GET"])(root_redirect)
    mcp.custom_route("/health", methods=["GET"])(health_check)

    # Auth
    mcp.custom_route("/auth/status", methods=["GET"])(auth_status)
    mcp.custom_route("/auth/setup", methods=["POST"])(auth_setup_endpoint)
    mcp.custom_route("/auth/login", methods=["POST"])(auth_login)
    mcp.custom_route("/auth/logout", methods=["POST"])(auth_logout)
    mcp.custom_route("/auth/change-password", methods=["POST"])(auth_change_password)

    # Hooks
    mcp.custom_route("/breath-hook", methods=["GET"])(breath_hook)
    mcp.custom_route("/dream-hook", methods=["GET"])(dream_hook)
    mcp.custom_route("/wakeup", methods=["GET"])(wakeup_hook)
    mcp.custom_route("/bot-context", methods=["GET"])(bot_context)

    # Dashboard & pages
    mcp.custom_route("/dashboard", methods=["GET"])(dashboard)
    mcp.custom_route("/desire", methods=["GET"])(desire_panel)

    # Dashboard API
    mcp.custom_route("/api/buckets", methods=["GET"])(api_buckets)
    mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])(api_bucket_detail)
    mcp.custom_route("/api/bot-visible", methods=["POST"])(api_bucket_bot_visible)
    mcp.custom_route("/api/search", methods=["GET"])(api_search)
    mcp.custom_route("/api/network", methods=["GET"])(api_network)
    mcp.custom_route("/api/breath-debug", methods=["GET"])(api_breath_debug)
    mcp.custom_route("/api/repair-unpinned", methods=["GET", "POST"])(api_repair_unpinned)
    mcp.custom_route("/api/desire/state", methods=["GET"])(api_desire_state)
    mcp.custom_route("/api/config", methods=["GET"])(api_config_get)
    mcp.custom_route("/api/config", methods=["POST"])(api_config_update)
    mcp.custom_route("/api/status", methods=["GET"])(api_system_status)

    # Host vault
    mcp.custom_route("/api/host-vault", methods=["GET"])(api_host_vault_get)
    mcp.custom_route("/api/host-vault", methods=["POST"])(api_host_vault_set)

    # Import
    mcp.custom_route("/api/import/upload", methods=["POST"])(api_import_upload)
    mcp.custom_route("/api/import/status", methods=["GET"])(api_import_status)
    mcp.custom_route("/api/import/pause", methods=["POST"])(api_import_pause)
    mcp.custom_route("/api/import/patterns", methods=["GET"])(api_import_patterns)
    mcp.custom_route("/api/import/results", methods=["GET"])(api_import_results)
    mcp.custom_route("/api/import/review", methods=["POST"])(api_import_review)

    # Calendar/Timeline/Note
    mcp.custom_route("/api/calendar-summary", methods=["GET"])(api_calendar_summary)
    mcp.custom_route("/api/timeline", methods=["GET"])(api_timeline)
    mcp.custom_route("/api/note", methods=["POST"])(api_note_create)

    # Export/Restore
    mcp.custom_route("/api/export-all", methods=["GET"])(api_export_all)
    mcp.custom_route("/api/import-restore", methods=["GET", "POST"])(api_import_restore)


# =============================================================
# Auth helpers
# =============================================================
def _get_auth_file() -> str:
    return os.path.join(_config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request):
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# =============================================================
# Auth endpoints
# =============================================================
async def auth_status(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


async def auth_setup_endpoint(request):
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


async def auth_login(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    return JSONResponse({"error": "密码错误"}, status_code=401)


async def auth_logout(request):
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


async def auth_change_password(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# Health & Root
# =============================================================
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await _bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if _decay_engine.is_running else "stopped",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# Hooks
# =============================================================
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        scored = sorted(unresolved, key=lambda b: _decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 10000
        for b in pinned:
            summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(f"📌 [核心准则] {summary}")
            token_budget -= count_tokens_approx(summary)

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        candidates = candidates[:20]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


async def wakeup_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        recent = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("resolved", False)
        ]
        recent.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = recent[:8]
        if not recent:
            return PlainTextResponse("")
        parts = []
        for b in recent:
            summary = await _dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(summary)
        body_text = "[Evan 醒来 - 最近的记忆]\n" + "\n---\n".join(parts)
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Wakeup hook failed: {e}")
        return PlainTextResponse("")


async def bot_context(request):
    """Return pinned buckets (bot_visible=true) + recent dynamic buckets for botchat.
    给botchat用的上下文端点：bot_visible的钉选桶 + 最近动态桶。"""
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)

        # --- Pinned buckets with bot_visible flag ---
        pinned_parts = []
        pinned_buckets = [
            b for b in all_buckets
            if (b["metadata"].get("pinned") or b["metadata"].get("protected"))
            and b["metadata"].get("bot_visible", False)
        ]
        for b in pinned_buckets:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(
                    strip_wikilinks(b["content"]), clean_meta
                )
                name = b["metadata"].get("name", b["id"])
                pinned_parts.append(f"[准则·{name}] {summary}")
            except Exception as e:
                logger.warning(f"Bot-context pinned dehydrate failed: {e}")

        # --- Recent dynamic buckets (same as wakeup but 12 instead of 8) ---
        dynamic_parts = []
        recent = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("resolved", False)
        ]
        recent.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = recent[:12]
        for b in recent:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await _dehydrator.dehydrate(
                    strip_wikilinks(b["content"]), clean_meta
                )
                dynamic_parts.append(summary)
            except Exception as e:
                logger.warning(f"Bot-context dynamic dehydrate failed: {e}")

        # --- Assemble ---
        parts = []
        if pinned_parts:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_parts))
        if dynamic_parts:
            parts.append("=== 最近记忆 ===\n" + "\n---\n".join(dynamic_parts))

        if not parts:
            return PlainTextResponse("")

        body_text = "[Evan 背景上下文]\n" + "\n\n".join(parts)
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Bot-context failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Dashboard pages
# =============================================================
async def dashboard(request):
    from starlette.responses import HTMLResponse
    dashboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


async def desire_panel(request):
    from starlette.responses import HTMLResponse
    panel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desire_panel.html")
    try:
        with open(panel_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception as e:
        return HTMLResponse(f"<pre>desire_panel.html missing: {e}</pre>", status_code=500)


# =============================================================
# Dashboard API
# =============================================================
async def api_buckets(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": _decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_bucket_detail(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await _bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": _decay_engine.calculate_score(meta),
    })


async def api_bucket_bot_visible(request):
    """Toggle bot_visible flag on a pinned bucket. Dashboard用。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    bucket_id = body.get("bucket_id", "")
    if not bucket_id:
        return JSONResponse({"error": "missing bucket_id"}, status_code=400)
    bucket = await _bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not bucket["metadata"].get("pinned"):
        return JSONResponse({"error": "只有钉选桶可以设置bot_visible"}, status_code=400)
    new_value = bool(body.get("bot_visible", False))
    success = await _bucket_mgr.update(bucket_id, bot_visible=new_value)
    if not success:
        return JSONResponse({"error": "更新失败"}, status_code=500)
    return JSONResponse({"ok": True, "bot_visible": new_value})


async def api_search(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        matches = await _bucket_mgr.search(query, limit=10)
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_network(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": _decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
            })
            if _embedding_engine and _embedding_engine.enabled:
                emb = await _embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb

        ids = list(embeddings.keys())
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                sim = _embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim > 0.5:
                    edges.append({"source": id_a, "target": id_b, "similarity": round(sim, 3)})

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_breath_debug(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None

    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": _bucket_mgr.w_topic,
            "emotion": _bucket_mgr.w_emotion,
            "time": _bucket_mgr.w_time,
            "importance": _bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic = _bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                emotion = _bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = _bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= _bucket_mgr.fuzzy_threshold,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]
        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": _bucket_mgr.fuzzy_threshold,
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_repair_unpinned(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        zombies = [
            b for b in all_buckets
            if b.get("metadata", {}).get("type") == "permanent"
            and not b.get("metadata", {}).get("pinned", False)
            and not b.get("metadata", {}).get("protected", False)
        ]
        preview = [
            {
                "id": b["id"],
                "name": b.get("metadata", {}).get("name", b["id"]),
                "domain": b.get("metadata", {}).get("domain", []),
                "importance": b.get("metadata", {}).get("importance", 5),
            }
            for b in zombies
        ]

        if request.method == "GET":
            return JSONResponse({
                "mode": "dry_run",
                "count": len(preview),
                "buckets": preview,
                "hint": "POST to this same endpoint to execute the repair",
            })

        repaired, failed = [], []
        for b in zombies:
            ok = await _bucket_mgr.update(b["id"], pinned=False)
            (repaired if ok else failed).append(b["id"])
        return JSONResponse({
            "mode": "executed",
            "repaired_count": len(repaired),
            "failed_count": len(failed),
            "repaired": repaired,
            "failed": failed,
            "note": "last_active refreshed to now; decay restarts from today",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_desire_state(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        await _desire_engine.ensure_started()
        return JSONResponse(_desire_engine.snapshot())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_config_get(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = _config.get("dehydration", {})
    emb = _config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": _config.get("merge_threshold", 75),
        "transport": _config.get("transport", "stdio"),
        "buckets_dir": _config.get("buckets_dir", ""),
    })


async def api_config_update(request):
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    if "dehydration" in body:
        d = body["dehydration"]
        dehy = _config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        _dehydrator.model = dehy.get("model", "deepseek-chat")
        _dehydrator.base_url = dehy.get("base_url", "")
        _dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(_dehydrator, "client") and _dehydrator.api_key:
            from openai import AsyncOpenAI
            _dehydrator.client = AsyncOpenAI(
                api_key=_dehydrator.api_key,
                base_url=_dehydrator.base_url,
            )

    if "embedding" in body:
        e = body["embedding"]
        emb = _config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            _embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            _embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    if "merge_threshold" in body:
        _config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


async def api_system_status(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await _bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if _decay_engine.is_running else "stopped",
            "embedding_enabled": _embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# Host vault
# =============================================================
def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def api_host_vault_get(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


async def api_host_vault_set(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API
# =============================================================
async def api_import_upload(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if _import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    async def _run_import():
        try:
            await _import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


async def api_import_status(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(_import_engine.get_status())


async def api_import_pause(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not _import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    _import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


async def api_import_patterns(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await _import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_import_results(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_import_review(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await _bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await _bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await _bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = _bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# Calendar / Timeline / Note
# =============================================================
async def api_calendar_summary(request):
    from starlette.responses import JSONResponse
    from collections import defaultdict
    err = _require_auth(request)
    if err: return err
    try:
        year = int(request.query_params.get("year", "2026"))
        month = int(request.query_params.get("month", "6"))
        prefix = f"{year}-{month:02d}"

        all_buckets = await _bucket_mgr.list_all(include_archive=False)
        days = defaultdict(lambda: {"count": 0, "types": defaultdict(int), "valence_sum": 0.0})

        for b in all_buckets:
            created = b["metadata"].get("created", "")
            if not str(created).startswith(prefix):
                continue
            day_key = str(created)[:10]
            entry = days[day_key]
            entry["count"] += 1
            btype = b["metadata"].get("type", "dynamic")
            entry["types"][btype] += 1
            entry["valence_sum"] += float(b["metadata"].get("valence", 0.5))

        result = {}
        for day_key, entry in days.items():
            avg_v = round(entry["valence_sum"] / entry["count"], 2) if entry["count"] else 0.5
            result[day_key] = {
                "count": entry["count"],
                "types": dict(entry["types"]),
                "avg_valence": avg_v,
            }
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_timeline(request):
    from starlette.responses import JSONResponse
    from collections import defaultdict
    err = _require_auth(request)
    if err: return err
    try:
        year = request.query_params.get("year", "")
        month = request.query_params.get("month", "")
        limit = int(request.query_params.get("limit", "200"))

        all_buckets = await _bucket_mgr.list_all(include_archive=False)

        if year and month:
            prefix = f"{int(year)}-{int(month):02d}"
            all_buckets = [b for b in all_buckets if str(b["metadata"].get("created", "")).startswith(prefix)]

        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        all_buckets = all_buckets[:limit]

        days = defaultdict(list)
        for b in all_buckets:
            created = str(b["metadata"].get("created", ""))
            day_key = created[:10] if len(created) >= 10 else "unknown"
            days[day_key].append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "type": b["metadata"].get("type", "dynamic"),
                "domain": b["metadata"].get("domain", []),
                "valence": b["metadata"].get("valence", 0.5),
                "arousal": b["metadata"].get("arousal", 0.3),
                "importance": b["metadata"].get("importance", 5),
                "created": created,
                "snippet": b["content"][:200] if b.get("content") else "",
                "pinned": b["metadata"].get("pinned", False),
                "resolved": b["metadata"].get("resolved", False),
            })

        sorted_days = dict(sorted(days.items(), reverse=True))
        return JSONResponse(sorted_days)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_note_create(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "内容不能为空"}, status_code=400)

    title = (body.get("title") or "").strip() or None

    bucket_id = await _bucket_mgr.create(
        content=content,
        bucket_type="note",
        name=title,
        tags=["鸿湍笔记"],
        importance=5,
        valence=0.5,
        arousal=0.3,
    )

    return JSONResponse({"id": bucket_id, "created": True})


# =============================================================
# Export / Restore
# =============================================================
async def api_export_all(request):
    from starlette.responses import Response, JSONResponse
    import io
    import zipfile
    import datetime

    err = _require_auth(request)
    if err:
        return err

    try:
        all_buckets = await _bucket_mgr.list_all(include_archive=True)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for b in all_buckets:
                bucket_data = {
                    "id": b["id"],
                    "metadata": b.get("metadata", {}),
                    "content": b.get("content", ""),
                }
                try:
                    emb = await _embedding_engine.get_embedding(b["id"])
                    if emb:
                        bucket_data["embedding"] = emb
                except Exception:
                    pass
                bucket_type = b.get("metadata", {}).get("type", "dynamic")
                filename = f"{bucket_type}/{b['id']}.json"
                zf.writestr(
                    filename,
                    _json_lib.dumps(bucket_data, ensure_ascii=False, indent=2),
                )

            manifest = {
                "exported_at": datetime.datetime.now().isoformat(),
                "total_buckets": len(all_buckets),
                "version": "1.4.1",
                "includes_embeddings": True,
                "counts_by_type": {},
            }
            for b in all_buckets:
                t = b.get("metadata", {}).get("type", "dynamic")
                manifest["counts_by_type"][t] = manifest["counts_by_type"].get(t, 0) + 1
            zf.writestr(
                "manifest.json",
                _json_lib.dumps(manifest, ensure_ascii=False, indent=2),
            )

        zip_buffer.seek(0)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ombre-brain-backup-{timestamp}.zip"

        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    except Exception as e:
        logger.error(f"Export failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_import_restore(request):
    from starlette.responses import HTMLResponse, JSONResponse
    import io
    import zipfile

    err = _require_auth(request)
    if err:
        return err

    # --- GET: serve upload page ---
    if request.method == "GET":
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Ombre Brain - Restore</title>
<style>
body { font-family: system-ui; max-width: 600px; margin: 60px auto; padding: 20px; background: #1a1a1a; color: #e0e0e0; }
h1 { font-size: 1.4em; color: #c9a96e; }
.warn { background: #3a2a1a; border: 1px solid #c9a96e; border-radius: 8px; padding: 16px; margin: 20px 0; font-size: 0.9em; line-height: 1.6; }
input[type=file] { margin: 20px 0; }
button { background: #c9a96e; color: #1a1a1a; border: none; padding: 10px 24px; border-radius: 6px; cursor: pointer; font-weight: bold; }
button:hover { background: #d4b87a; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
#result { margin-top: 20px; padding: 16px; border-radius: 8px; display: none; white-space: pre-wrap; font-family: monospace; font-size: 0.85em; line-height: 1.5; }
.ok { background: #1a2a1a; border: 1px solid #4a8; }
.err { background: #2a1a1a; border: 1px solid #a44; }
</style></head><body>
<h1>🧠 Ombre Brain — Restore from Backup</h1>
<div class="warn">
⚠ This will <b>add or overwrite</b> buckets from the zip into the current storage.<br>
Existing buckets with the same ID will be overwritten.<br>
Buckets not in the zip will be left untouched.
</div>
<input type="file" id="f" accept=".zip">
<br><button id="btn" onclick="upload()">Upload & Restore</button>
<div id="result"></div>
<script>
async function upload() {
  const f = document.getElementById('f').files[0];
  if (!f) { alert('Pick a zip file first'); return; }
  const btn = document.getElementById('btn');
  const res = document.getElementById('result');
  btn.disabled = true; btn.textContent = 'Restoring...';
  res.style.display = 'none';
  try {
    const fd = new FormData(); fd.append('file', f);
    const r = await fetch('/api/import-restore', { method: 'POST', body: fd });
    const j = await r.json();
    res.style.display = 'block';
    if (r.ok) { res.className = 'ok'; res.textContent = JSON.stringify(j, null, 2); }
    else { res.className = 'err'; res.textContent = 'Error: ' + JSON.stringify(j, null, 2); }
  } catch(e) { res.style.display = 'block'; res.className = 'err'; res.textContent = 'Network error: ' + e; }
  btn.disabled = false; btn.textContent = 'Upload & Restore';
}
</script></body></html>"""
        return HTMLResponse(html)

    # --- POST: restore from zip ---
    try:
        form = await request.form()
        upload_file = form.get("file")
        if not upload_file:
            return JSONResponse({"error": "No file uploaded"}, status_code=400)

        zip_bytes = await upload_file.read()
        zip_buffer = io.BytesIO(zip_bytes)

        if not zipfile.is_zipfile(zip_buffer):
            return JSONResponse({"error": "Not a valid zip file"}, status_code=400)

        zip_buffer.seek(0)
        zf = zipfile.ZipFile(zip_buffer, "r")

        success = 0
        failed = 0
        skipped = 0
        embeddings_restored = 0
        errors = []

        for name in zf.namelist():
            if name == "manifest.json" or not name.endswith(".json"):
                continue

            try:
                raw = zf.read(name)
                data = _json_lib.loads(raw.decode("utf-8"))

                bucket_id = data.get("id")
                metadata = data.get("metadata", {})
                content = data.get("content", "")
                emb_vector = data.get("embedding")

                if not bucket_id or not content.strip():
                    skipped += 1
                    continue

                bucket_type = metadata.get("type", "dynamic")
                pinned = metadata.get("pinned", False)

                if bucket_type == "permanent" or pinned:
                    type_dir = _bucket_mgr.permanent_dir
                elif bucket_type == "feel":
                    type_dir = _bucket_mgr.feel_dir
                elif bucket_type == "note":
                    type_dir = _bucket_mgr.note_dir
                elif bucket_type == "archive":
                    type_dir = _bucket_mgr.archive_dir
                else:
                    type_dir = _bucket_mgr.dynamic_dir

                domain = metadata.get("domain", [])
                if bucket_type == "feel":
                    primary_domain = "沉淀物"
                elif bucket_type == "note":
                    primary_domain = "鸿湍笔记"
                else:
                    primary_domain = sanitize_name(domain[0]) if domain else "未分类"

                target_dir = os.path.join(type_dir, primary_domain)
                os.makedirs(target_dir, exist_ok=True)

                post = frontmatter.Post(content, **metadata)
                bucket_name = metadata.get("name", bucket_id)
                if bucket_name and bucket_name != bucket_id:
                    fn = f"{sanitize_name(bucket_name)}_{bucket_id}.md"
                else:
                    fn = f"{bucket_id}.md"

                existing = _bucket_mgr._find_bucket_file(bucket_id)
                if existing:
                    file_path = existing
                else:
                    file_path = os.path.join(target_dir, fn)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))

                if emb_vector and _embedding_engine and _embedding_engine.enabled:
                    try:
                        _embedding_engine._store_embedding(bucket_id, emb_vector)
                        embeddings_restored += 1
                    except Exception as emb_err:
                        logger.warning(f"Embedding restore failed for {bucket_id}: {emb_err}")

                success += 1

            except Exception as e:
                failed += 1
                errors.append(f"{name}: {str(e)[:80]}")

        zf.close()

        result = {
            "status": "ok",
            "restored": success,
            "failed": failed,
            "skipped": skipped,
            "embeddings_restored": embeddings_restored,
        }
        if errors:
            result["errors"] = errors[:20]

        logger.info(f"Import-restore complete: {success} restored, {failed} failed, {skipped} skipped, {embeddings_restored} embeddings")
        return JSONResponse(result)

    except Exception as e:
        logger.error(f"Import-restore failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
