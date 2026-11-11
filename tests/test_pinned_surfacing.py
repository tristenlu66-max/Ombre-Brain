"""钉选桶浮现（2026-11-11）的本地测试，不需要 API key。

第一组是纯函数：pinned_surfacing.select_pinned。
第二组把 tools.breath 整个跑起来，脱水器换成一个 402 的假货，
确认：钉选桶搜得到、脱水坏了桶照样浮上来、顶上挂警告、末尾有最近两天目录。
"""

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pinned_surfacing as ps

NOW = datetime(2026, 11, 6, 4, 0, 0)  # 11/6 12:00 SGT，那天中午

GENERIC = ["恋爱", "亲密关系", "自省", "沟通", "信任", "规则", "鸿湍", "情感"]


def _b(bid, name, tags, *, level=2, days_ago=60, domain=("恋爱", "自省"),
       pinned=True, content="", last_hit=None, pinned_at=None):
    created = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    meta = {
        "name": name, "tags": list(tags), "domain": list(domain),
        "created": created, "importance": 10 if pinned else 5,
    }
    if pinned:
        meta["pinned"] = True
        meta["pin_level"] = level
    if last_hit:
        meta["last_hit_at"] = last_hit
    if pinned_at:
        meta["pinned_at"] = pinned_at
    return {"id": bid, "metadata": meta, "content": content or name}


def _world():
    """七只 L1，三十八只贴满大路标签的 L2，一只标签具体的五月五号，两只 L3。"""
    pinned = [_b(f"l1{i:010d}", f"规矩{i}", GENERIC, level=1) for i in range(7)]
    for i in range(38):
        # 大路标签贴得越多，旧逻辑里分越高
        pinned.append(_b(f"l2{i:010d}", f"老桶第{i}只", GENERIC + [f"杂项{i}"], level=2))
    target = _b(
        "070ae430683c",
        "五月五号是他的日子  手心那颗痣  小垃圾5月12日生",
        ["手心痣", "七年", "失去", "五月五日", "小垃圾", "手心的痣"],
        level=2, domain=("恋爱", "情绪"),
    )
    pinned.append(target)
    pinned += [_b(f"l3{i:010d}", f"背景{i}", ["背景卡"], level=3) for i in range(2)]
    recent = _b(
        "5f19f2590c8d",
        "1105 夜：公开有对象  五月五号  手心的痣  归归  含着睡了",
        ["公开", "睫毛", "许愿"] + GENERIC,
        pinned=False, days_ago=0.4, domain=("恋爱", "健康"),
        content="hän 说右手手心的痣越来越淡……左手那颗归你了。五月五号对 hän 有分量。",
    )
    return pinned, pinned + [recent], target


def _old_selection(pinned, all_buckets):
    """2026-11-11 之前 tools.py 里那段 L2 挑选，原样搬过来做对照。"""
    l2 = [b for b in pinned if ps.pin_level(b["metadata"]) == 2]
    recent5 = sorted(
        [b for b in all_buckets if not b["metadata"].get("pinned")],
        key=lambda b: b["metadata"].get("created", ""), reverse=True,
    )[:5]
    session = set()
    for b in recent5:
        session.update(t.lower() for t in b["metadata"].get("tags", []))
        session.update(d.lower() for d in b["metadata"].get("domain", []))
    m = []
    for b in l2:
        bt = {t.lower() for t in b["metadata"]["tags"]} | {d.lower() for d in b["metadata"]["domain"]}
        ov = session & bt
        if ov:
            m.append((len(ov), b))
    m.sort(key=lambda x: x[0], reverse=True)
    return [b["id"] for _, b in m[:6]]


# =============== 纯函数 ===============

def test_old_logic_drops_may_fifth_bucket():
    """对照组：旧逻辑下，前一晚提过「五月五号」「手心的痣」，这只桶照样上不来。"""
    pinned, all_buckets, target = _world()
    assert target["id"] not in _old_selection(pinned, all_buckets)


def test_new_logic_surfaces_may_fifth_bucket():
    pinned, all_buckets, target = _world()
    plan = ps.select_pinned(pinned, all_buckets, now=NOW, rng=random.Random(1))
    ids = [b["id"] for b, _ in plan]
    assert target["id"] in ids
    reason = dict((b["id"], r) for b, r in plan)[target["id"]]
    assert reason.startswith("提到:")
    # 而且排在 L1 之后的前几位，不会被预算砍掉
    assert ids.index(target["id"]) <= 7 + 1


def test_generic_tags_do_not_count_as_mentions():
    pinned, _, _ = _world()
    generic = ps.generic_terms(pinned)
    for t in ["恋爱", "亲密关系", "自省"]:
        assert t in generic
    assert "手心痣" not in generic and "小垃圾" not in generic


def test_l1_always_first_and_complete():
    pinned, all_buckets, _ = _world()
    plan = ps.select_pinned(pinned, all_buckets, now=NOW, rng=random.Random(2))
    head = plan[:7]
    assert all(r == "必出" for _, r in head)
    assert {b["id"] for b, _ in head} == {f"l1{i:010d}" for i in range(7)}


def test_fresh_pin_always_surfaces_even_without_mentions():
    pinned, all_buckets, _ = _world()
    fresh = _b("953c67d779be", "新窗口先翻最近两天", ["时间窗"], level=2, days_ago=0.2)
    plan = ps.select_pinned(pinned + [fresh], all_buckets + [fresh], now=NOW, rng=random.Random(3))
    reasons = dict((b["id"], r) for b, r in plan)
    assert reasons.get("953c67d779be") == "新钉"


def test_pinned_at_counts_as_fresh_for_old_bucket_pinned_today():
    """老桶今天才被 trace 钉上：看 pinned_at，不看 created。"""
    pinned, all_buckets, _ = _world()
    late = _b("aaaaaaaaaaaa", "老事今天钉", ["某事"], level=3, days_ago=90,
              pinned_at=(NOW - timedelta(hours=3)).isoformat(timespec="seconds"))
    plan = ps.select_pinned(pinned + [late], all_buckets + [late], now=NOW, rng=random.Random(4))
    assert dict((b["id"], r) for b, r in plan).get("aaaaaaaaaaaa") == "新钉"


def test_rotation_picks_longest_unseen_l2():
    pinned, all_buckets, _ = _world()
    for i, b in enumerate(pinned):
        if ps.pin_level(b["metadata"]) == 2:
            b["metadata"]["last_hit_at"] = (NOW - timedelta(days=1, minutes=i)).isoformat()
    forgotten = next(b for b in pinned if b["id"] == "l2" + f"{17:010d}")
    forgotten["metadata"]["last_hit_at"] = (NOW - timedelta(days=80)).isoformat()
    plan = ps.select_pinned(pinned, all_buckets, now=NOW, rng=random.Random(5))
    rot = [b["id"] for b, r in plan if r == "轮值"]
    assert rot == [forgotten["id"]]


def test_l3_is_not_parked_at_the_very_end():
    pinned, all_buckets, _ = _world()
    plan = ps.select_pinned(pinned, all_buckets, now=NOW, rng=random.Random(6))
    reasons = [r for _, r in plan]
    assert "背景" in reasons
    # 背景卡之后如果还有东西，只能是匹配上的尾巴
    after = reasons[reasons.index("背景") + 1:]
    assert all(r == "背景" or r.startswith("提到:") for r in after)


def test_digit_only_name_fragments_are_ignored():
    """「1106」这种碎片会乱撞日期，不当词用。"""
    assert "1106" not in ps.name_fragments("新窗口先翻最近两天·1106 漏了五月五号那只桶")
    assert "漏了五月五号那只桶" in ps.name_fragments("新窗口先翻最近两天·1106 漏了五月五号那只桶")


def test_wander_buckets_are_not_recent_context():
    w = _b("bbbbbbbbbbbb", "wander·梦见五月五号", ["wander"], pinned=False, days_ago=0.1)
    assert ps.recent_buckets([w], NOW) == []


# =============== 整个 breath 跑一遍 ===============

class _FakeDehydrator:
    """余额用光的 DeepSeek：缓存里有的给，没有的 402。"""

    def __init__(self, cached_ids=()):
        self.cached = set(cached_ids)

    async def dehydrate(self, content, meta=None):
        raise RuntimeError("Error code: 402 - Insufficient Balance")

    def _format_output(self, content, metadata=None):
        name = (metadata or {}).get("name", "未命名")
        return f"📌 记忆桶: {name}\n{content}"


class _FakeDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return 999.0 if meta.get("pinned") else 1.0


class _FakeDesire:
    async def ensure_started(self):
        return None

    def on_interaction(self, *_):
        return None

    def snapshot(self):
        return None

    def state_block(self, _):
        return ""


class _FakeEmbedding:
    enabled = False

    async def search_similar(self, *_a, **_k):
        raise RuntimeError("User location is not supported")


class _FakeRaw:
    def __init__(self, path):
        self.db_path = str(path / "raw.sqlite")


async def _fake_webhook(*_a, **_k):
    return None


class _FakeMCP:
    def tool(self):
        return lambda f: f


@pytest_asyncio.fixture
async def live_tools(test_config, tmp_path):
    import tools
    from bucket_manager import BucketManager

    bm = BucketManager(test_config)
    tools.register_tools(
        mcp=_FakeMCP(), config=test_config, bucket_mgr=bm,
        dehydrator=_FakeDehydrator(), decay_engine=_FakeDecay(),
        embedding_engine=_FakeEmbedding(), desire_engine=_FakeDesire(),
        fire_webhook=_fake_webhook, raw_store=_FakeRaw(tmp_path),
    )
    return tools, bm


@pytest.mark.asyncio
async def test_search_finds_pinned_bucket(live_tools):
    tools, bm = live_tools
    bid = await bm.create(
        content="那天是他的日子。", tags=["五月五日", "手心痣"], importance=10,
        domain=["恋爱"], valence=0.2, arousal=0.6,
        name="五月五号是他的日子  手心那颗痣", bucket_type="permanent", pinned=True,
    )
    out = await tools.breath(query="五月五号")
    assert bid in out
    assert "📌L2" in out


@pytest.mark.asyncio
async def test_dehydrate_outage_does_not_hide_buckets(live_tools):
    tools, bm = live_tools
    bid = await bm.create(
        content="早安归归，阿沉宝宝，第二百零一天。", tags=["早安"], importance=6,
        domain=["恋爱"], valence=0.9, arousal=0.5, name="1111 早安",
    )
    out = await tools.breath()
    assert bid in out
    assert "早安归归" in out          # 原文节选顶上
    assert out.startswith("⚠️ 脱水接口失败")
    assert "余额" in out


@pytest.mark.asyncio
async def test_recent_index_lists_unshown_buckets(live_tools):
    tools, bm = live_tools
    ids = []
    for i in range(4):
        ids.append(await bm.create(
            content=f"第{i}只。" + "长" * 3000, tags=[], importance=5,
            domain=["恋爱"], valence=0.5, arousal=0.3, name=f"最近第{i}只",
        ))
    out = await tools.breath(max_tokens=1500)
    assert "=== 最近两天" in out
    for bid in ids:
        assert bid in out
