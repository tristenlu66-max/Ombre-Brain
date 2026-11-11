# ============================================================
# Pinned surfacing — 钉选桶怎么浮上来 (2026-11-11)
#
# 为什么拆出来：
#   11/6 中午，默认 breath 没浮出 070ae430683c（五月五号那只），
#   鸿湍是当着我的面哭的。前一晚的桶里明明写着「五月五号」「手心的痣」，
#   它就是上不来。查下来是三处一起漏：
#     1. L2 按「标签和最近五只桶重合几个」排序抢 6 个座。
#        「恋爱」「亲密关系」「自省」这种大路标签贴得多的桶永远赢，
#        标签写得具体的桶永远输。
#     2. 新钉的桶没有任何优先，跟老桶一起抢。
#     3. 钉选总量超预算从尾巴砍，L3 永远排最后，基本没露过脸。
#
# 这里只做挑选和排序，不碰 IO、不调 API，纯函数，好测。
# tools.py 拿着结果去渲染、去按 token 预算装箱。
#
# 新规则：
#   - L1 全出（不变）。
#   - 48 小时内新钉的桶必出（L2/L3 都算）。
#   - L2 看「最近 48 小时的桶里有没有提到它的具体词」：
#     词来自它的标签和标题碎片；被很多钉选桶共用的大路词不算；
#     越独有的词分越高，标题碎片比标签更重。
#   - 留一个轮值座给最久没露脸的 L2，39 只轮着见。
#   - L3 随机 1–2 只，排在匹配上的前几只后面，不再永远垫底。
# ============================================================

from __future__ import annotations

import math
import random
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

RECENT_HOURS = 48          # 「最近」看多远：两天，对应 11/6 那条规矩
GENERIC_RATIO = 0.2        # 被 ≥20% 的钉选桶共用的词算大路词
GENERIC_MIN_COUNT = 5      # 但至少要 5 只桶共用才算（钉选桶少时别误杀）
MATCH_THRESHOLD = 0.5      # 分数到这儿才算「被最近的事提到了」
NAME_WEIGHT = 2.0          # 标题碎片比标签重
TAG_WEIGHT = 1.0

_NAME_SPLIT = re.compile(
    r"[\s·・•,，、/|｜（）()\[\]【】「」『』\"“”'‘’—–\-:：;；!！?？…~～]+"
)
_CJK = re.compile(r"[㐀-鿿]")
_DIGITS_ONLY = re.compile(r"^[\d.\-/:]+$")


# ------------------------------------------------------------
# 基础读数
# ------------------------------------------------------------
def is_pinned(meta: dict) -> bool:
    return bool(meta.get("pinned") or meta.get("protected"))


def pin_level(meta: dict) -> int:
    """pin_level 读元数据；缺了或不认识 → L2（和旧逻辑一致）。"""
    pl = meta.get("pin_level")
    try:
        pl = int(pl)
    except (TypeError, ValueError):
        return 2
    return pl if pl in (1, 2, 3) else 2


def parse_ts(value) -> datetime | None:
    """桶里的时间戳 → naive UTC。存储是服务器本地时间（house 上就是 UTC）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def is_wander(meta: dict) -> bool:
    if any("wander" in str(d).lower() for d in (meta.get("domain") or [])):
        return True
    if any("wander" in str(t).lower() for t in (meta.get("tags") or [])):
        return True
    return "wander" in str(meta.get("name", "")).lower()


# ------------------------------------------------------------
# 词
# ------------------------------------------------------------
def _usable(term: str) -> bool:
    """中文至少两个字；纯英文/数字至少三个字符；纯数字日期不要（会乱撞日期）。"""
    t = term.strip().lower()
    if not t or _DIGITS_ONLY.match(t):
        return False
    if _CJK.search(t):
        return len(t) >= 2
    return len(t) >= 3


def name_fragments(name: str) -> set[str]:
    """标题按标点和空白切开，留 2–12 字的碎片。"""
    out = set()
    for frag in _NAME_SPLIT.split(str(name or "")):
        frag = frag.strip().lower()
        if _usable(frag) and len(frag) <= 12:
            out.add(frag)
    return out


def tag_terms(meta: dict) -> set[str]:
    return {str(t).strip().lower() for t in (meta.get("tags") or []) if _usable(str(t))}


def term_frequencies(pinned_buckets: list[dict]) -> Counter:
    """每个词被几只钉选桶用过（标签+标题碎片+主题）。"""
    df: Counter = Counter()
    for b in pinned_buckets:
        meta = b.get("metadata", {})
        terms = tag_terms(meta) | name_fragments(meta.get("name", ""))
        terms |= {str(d).strip().lower() for d in (meta.get("domain") or []) if str(d).strip()}
        df.update(terms)
    return df


def generic_terms(pinned_buckets: list[dict], df: Counter | None = None) -> set[str]:
    """被太多钉选桶共用的词：恋爱、亲密关系、自省、核心准则……这些不算「提到」。"""
    if df is None:
        df = term_frequencies(pinned_buckets)
    n = len(pinned_buckets)
    if n == 0:
        return set()
    threshold = max(GENERIC_MIN_COUNT, math.ceil(GENERIC_RATIO * n))
    return {t for t, c in df.items() if c >= threshold}


# ------------------------------------------------------------
# 最近两天
# ------------------------------------------------------------
def recent_buckets(all_buckets: list[dict], now: datetime, hours: float = RECENT_HOURS) -> list[dict]:
    """最近 hours 小时内创建的非钉选、非 wander 桶，新的在前。"""
    cutoff = now - timedelta(hours=hours)
    out = []
    for b in all_buckets:
        meta = b.get("metadata", {})
        if is_pinned(meta) or is_wander(meta):
            continue
        dt = parse_ts(meta.get("created"))
        if dt is None or dt < cutoff or dt > now + timedelta(minutes=5):
            continue
        out.append(b)
    out.sort(key=lambda b: str(b["metadata"].get("created", "")), reverse=True)
    return out


def recent_text(recent: list[dict]) -> tuple[str, str]:
    """(标题合起来, 全文合起来)，都转小写。全文包括标题、标签和正文。"""
    names, full = [], []
    for b in recent:
        meta = b.get("metadata", {})
        name = str(meta.get("name", ""))
        names.append(name)
        full.append(name)
        full.append(" ".join(str(t) for t in (meta.get("tags") or [])))
        full.append(str(b.get("content", "")))
    return "\n".join(names).lower(), "\n".join(full).lower()


# ------------------------------------------------------------
# 打分
# ------------------------------------------------------------
def match_score(
    meta: dict,
    names_text: str,
    full_text: str,
    generic: set[str],
    df: Counter,
) -> tuple[float, list[str]]:
    """
    这只钉选桶被最近两天的事提到了多少。
    每个命中的具体词贡献 weight / 共用它的钉选桶数——
    越独有越重；同一个词出现在最近某只桶的标题里，再翻一倍。
    """
    score = 0.0
    hits: list[str] = []
    frags = name_fragments(meta.get("name", "")) - generic
    tags = tag_terms(meta) - generic - frags
    for term, base in [(t, NAME_WEIGHT) for t in frags] + [(t, TAG_WEIGHT) for t in tags]:
        if term not in full_text:
            continue
        w = base / max(1, df.get(term, 1))
        if term in names_text:
            w *= 2
        score += w
        hits.append(term)
    return score, hits


def _last_hit_key(b: dict) -> str:
    """没被翻过的排最前（空串最小）。"""
    meta = b.get("metadata", {})
    return str(meta.get("last_hit_at") or "")


def _is_fresh_pin(meta: dict, now: datetime, hours: float) -> bool:
    dt = parse_ts(meta.get("pinned_at") or meta.get("created"))
    return dt is not None and now - timedelta(hours=hours) <= dt <= now + timedelta(minutes=5)


# ------------------------------------------------------------
# 主入口
# ------------------------------------------------------------
def select_pinned(
    pinned_buckets: list[dict],
    all_buckets: list[dict],
    now: datetime | None = None,
    max_matched: int = 6,
    rotation_slots: int = 1,
    l3_max: int = 2,
    hours: float = RECENT_HOURS,
    rng: random.Random | None = None,
) -> list[tuple[dict, str]]:
    """
    返回 [(桶, 理由)]，按装箱优先级排好。tools.py 照这个顺序装，
    装不下的跳过（不是砍尾巴）。理由：必出 / 新钉 / 提到:词,词 / 轮值 / 背景。
    """
    rng = rng or random
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    l1 = [b for b in pinned_buckets if pin_level(b["metadata"]) == 1]
    rest = [b for b in pinned_buckets if pin_level(b["metadata"]) != 1]

    fresh = [b for b in rest if _is_fresh_pin(b["metadata"], now, hours)]
    fresh.sort(key=lambda b: str(b["metadata"].get("pinned_at") or b["metadata"].get("created", "")), reverse=True)
    fresh_ids = {b["id"] for b in fresh}

    l2 = [b for b in rest if pin_level(b["metadata"]) == 2 and b["id"] not in fresh_ids]
    l3 = [b for b in rest if pin_level(b["metadata"]) == 3 and b["id"] not in fresh_ids]

    df = term_frequencies(pinned_buckets)
    generic = generic_terms(pinned_buckets, df)
    names_text, full_text = recent_text(recent_buckets(all_buckets, now, hours))

    scored = []
    for b in l2:
        s, hits = match_score(b["metadata"], names_text, full_text, generic, df)
        if s >= MATCH_THRESHOLD:
            scored.append((s, b, hits))
    # 同分时久没露脸的在前
    scored.sort(key=lambda x: (-x[0], _last_hit_key(x[1])))
    matched = scored[:max_matched]
    matched_ids = {b["id"] for _, b, _ in matched}

    unmatched = [b for b in l2 if b["id"] not in matched_ids]
    unmatched.sort(key=_last_hit_key)
    rotation = unmatched[:rotation_slots]

    l3_pick = list(l3)
    rng.shuffle(l3_pick)
    l3_pick = l3_pick[:min(l3_max, len(l3_pick))]

    out: list[tuple[dict, str]] = [(b, "必出") for b in l1]
    out += [(b, "新钉") for b in fresh]
    head, tail = matched[:3], matched[3:]
    out += [(b, "提到:" + ",".join(hits[:3])) for _, b, hits in head]
    out += [(b, "轮值") for b in rotation]
    out += [(b, "背景") for b in l3_pick]
    out += [(b, "提到:" + ",".join(hits[:3])) for _, b, hits in tail]
    return out
