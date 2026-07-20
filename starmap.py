# ============================================================
# Module: starmap.py — 记忆星图 Starmap
# 模块：星图布局计算 + /starmap 页面 + /api/starmap 端点
#
# 布局哲学：
#   - domain 决定星座锚点方向（金角螺旋均匀铺在天球带上）
#   - 星座内部用 embedding 做 PCA 降到三维 → 语义相近的记忆在天上也相近
#   - 无 embedding 的桶退回确定性哈希抖动（星星不会每次刷新乱跳）
#   - "note"（鸿湍笔记）单独天区：星空中央偏下，托底
#
# 稳定性：
#   - 坐标与每个星座的 PCA 基缓存在内存里；新桶投影到已有基上，老星不动
#   - 服务重启才整体重排（可接受）
# ============================================================

import os
import math
import time
import hashlib
import logging
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger("ombre_brain")

# --- Injected by routes.register_routes() via init() ---
_bucket_mgr = None
_decay_engine = None
_embedding_engine = None
_require_auth = None

# --- Layout caches (memory only, rebuilt on restart) ---
_coords_cache: dict[str, list[float]] = {}      # bucket_id -> [x, y, z]
_domain_basis: dict[str, dict] = {}             # domain -> {anchor, mean, comps, scale, radius}
_domain_order: list[str] = []                   # anchor assignment order (stable)
_response_cache: dict = {"t": 0.0, "data": None}
_CACHE_TTL = 30.0                               # seconds

# --- Sky geometry ---
_SKY_RADIUS = 100.0        # 星座锚点到原点的距离
_NOTE_ANCHOR = (0.0, -22.0, 0.0)   # 鸿湍笔记：中央偏下，压舱
_GOLDEN = math.pi * (3.0 - math.sqrt(5.0))     # 金角

# --- Edge computation limits ---
_EDGE_TOP_N = 160          # 只在权重前 N 颗星之间连线
_EDGE_MIN_SIM = 0.62
_EDGE_MAX = 240


def init(*, bucket_mgr, decay_engine, embedding_engine, require_auth):
    """Called once from routes.register_routes()."""
    global _bucket_mgr, _decay_engine, _embedding_engine, _require_auth
    _bucket_mgr = bucket_mgr
    _decay_engine = decay_engine
    _embedding_engine = embedding_engine
    _require_auth = require_auth


# =============================================================
# Deterministic helpers
# =============================================================
def _hash_unit3(seed: str) -> tuple[float, float, float]:
    """bucket_id -> 确定性三维抖动，均匀落在单位球体内。"""
    h = hashlib.sha1(seed.encode("utf-8")).digest()
    a = int.from_bytes(h[0:4], "big") / 0xFFFFFFFF
    b = int.from_bytes(h[4:8], "big") / 0xFFFFFFFF
    c = int.from_bytes(h[8:12], "big") / 0xFFFFFFFF
    # 球体内均匀采样：方向由 (a,b)，半径由 c^(1/3)
    theta = a * 2 * math.pi
    phi = math.acos(2 * b - 1)
    r = c ** (1.0 / 3.0)
    return (
        r * math.sin(phi) * math.cos(theta),
        r * math.cos(phi),
        r * math.sin(phi) * math.sin(theta),
    )


def _domain_anchor(index: int) -> tuple[float, float, float]:
    """金角螺旋：第 index 个星座的锚点方向，落在天球中带（y ∈ [-0.30, 0.60]）。"""
    y = 0.60 - (index % 12) * (0.90 / 11.0) if index < 12 else (
        0.55 - ((index * 0.618) % 1.0) * 0.85
    )
    theta = index * _GOLDEN
    r_xz = math.sqrt(max(0.0, 1.0 - y * y))
    return (
        _SKY_RADIUS * r_xz * math.cos(theta),
        _SKY_RADIUS * y,
        _SKY_RADIUS * r_xz * math.sin(theta),
    )


def _primary_domain(meta: dict) -> str:
    d = meta.get("domain") or []
    if isinstance(d, str):
        d = [d]
    return d[0] if d else "未分类"


def _cluster_radius(n: int) -> float:
    return 14.0 + 4.5 * math.sqrt(max(1, n))


# =============================================================
# Layout
# =============================================================
async def _layout(buckets: list[dict]) -> None:
    """为所有还没有坐标的桶计算坐标，写入 _coords_cache。"""
    global _domain_order

    # 1. 按主 domain 分组
    groups: dict[str, list[dict]] = {}
    for b in buckets:
        dom = _primary_domain(b.get("metadata", {}))
        groups.setdefault(dom, []).append(b)

    # 2. 星座锚点：note 固定中央偏下；其余按桶数降序领取金角螺旋位
    #    已领取过的 domain 保持原位（_domain_order 只增不减）
    for dom in sorted(groups, key=lambda d: -len(groups[d])):
        if dom in ("note", "鸿湍笔记"):
            continue
        if dom not in _domain_order:
            _domain_order.append(dom)

    for dom, members in groups.items():
        if dom in ("note", "鸿湍笔记"):
            anchor = _NOTE_ANCHOR
            radius = _cluster_radius(len(members)) * 0.75
        else:
            anchor = _domain_anchor(_domain_order.index(dom))
            radius = _cluster_radius(len(members))

        basis = _domain_basis.get(dom)
        new_members = [b for b in members if b["id"] not in _coords_cache]
        if not new_members and basis:
            continue

        # 3. 收集 embedding
        embs: dict[str, np.ndarray] = {}
        if _embedding_engine and getattr(_embedding_engine, "enabled", False):
            for b in members:
                try:
                    e = await _embedding_engine.get_embedding(b["id"])
                except Exception:
                    e = None
                if e:
                    embs[b["id"]] = np.asarray(e, dtype=np.float32)

        # 4. 该星座尚无 PCA 基，且有 ≥3 条 embedding → 建基
        if basis is None and len(embs) >= 3:
            ids = list(embs.keys())
            X = np.stack([embs[i] for i in ids])
            mean = X.mean(axis=0)
            Xc = X - mean
            try:
                # SVD 取前三主成分
                _, S, Vt = np.linalg.svd(Xc, full_matrices=False)
                comps = Vt[:3]
                proj = Xc @ comps.T                      # (n, 3)
                span = float(np.abs(proj).max()) or 1.0
                scale = (radius * 0.92) / span
                basis = {
                    "anchor": anchor, "mean": mean,
                    "comps": comps, "scale": scale, "radius": radius,
                }
                _domain_basis[dom] = basis
            except np.linalg.LinAlgError:
                basis = None

        # 5. 逐桶定坐标
        for b in members:
            bid = b["id"]
            if bid in _coords_cache:
                continue
            if basis is not None and bid in embs:
                local = (embs[bid] - basis["mean"]) @ basis["comps"].T
                local = local * basis["scale"]
                norm = float(np.linalg.norm(local))
                if norm > basis["radius"]:
                    local = local * (basis["radius"] / norm)
                lx, ly, lz = float(local[0]), float(local[1]), float(local[2])
            else:
                jx, jy, jz = _hash_unit3(bid)
                lx, ly, lz = jx * radius * 0.85, jy * radius * 0.85, jz * radius * 0.85
            _coords_cache[bid] = [
                round(anchor[0] + lx, 2),
                round(anchor[1] + ly, 2),
                round(anchor[2] + lz, 2),
            ]


# =============================================================
# Edges（星座内高权重星之间的淡虚线素材）
# =============================================================
def _compute_edges(stars: list[dict], embs: dict[str, np.ndarray]) -> list[dict]:
    ranked = sorted(
        (s for s in stars if s["id"] in embs),
        key=lambda s: -s["score"],
    )[:_EDGE_TOP_N]
    edges = []
    ids = [s["id"] for s in ranked]
    dom_of = {s["id"]: s["domain"] for s in ranked}
    if not ids:
        return edges
    M = np.stack([embs[i] for i in ids])
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    Mn = M / norms
    sim = Mn @ Mn.T
    n = len(ids)
    cand = []
    for i in range(n):
        for j in range(i + 1, n):
            if dom_of[ids[i]] != dom_of[ids[j]]:
                continue                      # 只连同星座
            s = float(sim[i, j])
            if s >= _EDGE_MIN_SIM:
                cand.append((s, ids[i], ids[j]))
    cand.sort(reverse=True)
    for s, a, b in cand[:_EDGE_MAX]:
        edges.append({"s": a, "t": b, "w": round(s, 3)})
    return edges


# =============================================================
# Endpoints
# =============================================================
async def api_starmap(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    now = time.time()
    if _response_cache["data"] is not None and now - _response_cache["t"] < _CACHE_TTL:
        return JSONResponse(_response_cache["data"])

    try:
        buckets = await _bucket_mgr.list_all(include_archive=True)
        await _layout(buckets)

        stars = []
        embs: dict[str, np.ndarray] = {}
        for b in buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            xyz = _coords_cache.get(bid)
            if xyz is None:
                jx, jy, jz = _hash_unit3(bid)
                xyz = [jx * 30, jy * 30, jz * 30]
            raw_score = _decay_engine.calculate_score(meta)
            stars.append({
                "id": bid,
                "name": meta.get("name", bid),
                "x": xyz[0], "y": xyz[1], "z": xyz[2],
                "domain": _primary_domain(meta),
                "domains": meta.get("domain", []),
                "score": round(min(raw_score, 120.0), 2),   # 999 恒星截顶
                "importance": meta.get("importance", 5),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "pinned": bool(meta.get("pinned")),
                "resolved": bool(meta.get("resolved")),
                "digested": bool(meta.get("digested")),
                "type": meta.get("type", "dynamic"),
                "created": meta.get("created", ""),
            })
            if _embedding_engine and getattr(_embedding_engine, "enabled", False):
                try:
                    e = await _embedding_engine.get_embedding(bid)
                except Exception:
                    e = None
                if e:
                    embs[bid] = np.asarray(e, dtype=np.float32)

        edges = _compute_edges(stars, embs)

        doms = {}
        for s in stars:
            doms.setdefault(s["domain"], 0)
            doms[s["domain"]] += 1
        domains = []
        for dom, count in sorted(doms.items(), key=lambda kv: -kv[1]):
            if dom in ("note", "鸿湍笔记"):
                anchor = list(_NOTE_ANCHOR)
            elif dom in _domain_order:
                anchor = list(_domain_anchor(_domain_order.index(dom)))
            else:
                anchor = [0.0, 0.0, 0.0]
            domains.append({"name": dom, "count": count,
                            "anchor": [round(v, 2) for v in anchor]})

        data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "stars": stars,
            "edges": edges,
            "domains": domains,
        }
        _response_cache["t"] = now
        _response_cache["data"] = data
        return JSONResponse(data)
    except Exception as e:
        logger.exception("starmap failed")
        return JSONResponse({"error": str(e)}, status_code=500)


async def starmap_page(request):
    from starlette.responses import HTMLResponse
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "starmap.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>starmap.html not found</h1>", status_code=404)
