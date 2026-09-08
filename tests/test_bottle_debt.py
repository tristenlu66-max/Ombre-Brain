"""奶瓶通道 + 欠条本的本地测试（不需要 API key）。"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decay_engine import DecayEngine, BOTTLE_DOMAIN, BOTTLE_FLOOR
from debts import DebtStore


def _meta(days_ago, **kw):
    t = (datetime.now() - timedelta(days=days_ago)).isoformat()
    base = {
        "importance": 6, "arousal": 0.5,
        "created": t, "last_active": t, "activation_count": 1,
        "domain": ["恋爱"],
    }
    base.update(kw)
    return base


# =============== 奶瓶：权重 ===============

def test_bottle_never_falls_below_floor():
    """奶瓶桶再老也不许掉到地板以下。这是整条道存在的理由。"""
    e = DecayEngine({}, None)
    ancient = _meta(400, domain=[BOTTLE_DOMAIN, "恋爱"], resolved=True, importance=5)
    assert e.calculate_score(ancient) >= BOTTLE_FLOOR


def test_bottle_beats_a_heavily_read_regret_bucket():
    """翻了 40 次的自省桶不该压过没人翻的奶瓶桶。"""
    e = DecayEngine({}, None)
    bottle = e.calculate_score(_meta(90, domain=[BOTTLE_DOMAIN]))
    regret = e.calculate_score(_meta(90, domain=["自省"], activation_count=40))
    assert bottle > regret


def test_bottle_does_not_change_other_buckets():
    """普通桶的算分一个字没动。"""
    e = DecayEngine({}, None)
    assert e.calculate_score(_meta(30)) == pytest.approx(1.714, abs=0.01)


def test_hit_count_term_is_zeroed_for_bottle():
    """被翻多少次都不影响奶瓶桶的分。"""
    e = DecayEngine({}, None)
    a = e.calculate_score(_meta(2, domain=[BOTTLE_DOMAIN], activation_count=1))
    b = e.calculate_score(_meta(2, domain=[BOTTLE_DOMAIN], activation_count=50))
    assert a == b


# =============== 奶瓶：写入闸门 ===============

def _check(content, tags):
    """把 check_bottle_write 从 tools 里取出来，避开运行时依赖。"""
    src = (Path(__file__).resolve().parent.parent / "tools.py").read_text()
    ns = {"logger": __import__("logging").getLogger("t")}
    exec(src[src.index("BOTTLE_BANNED_TAGS"):src.index("def _auto_merge_enabled")], ns)
    return ns["check_bottle_write"](content, tags)


def test_bottle_rejects_regret_tag():
    ok, err, _ = _check("hän 亲了带奶味的我", ["恋爱", "自省"])
    assert not ok and "自省" in err


def test_bottle_warns_but_allows_analysis():
    """留一条缝：分析句式只告警，不拦。但要打出来让我看见。"""
    ok, _, warn = _check("这说明 hän 在乎我", ["恋爱"])
    assert ok and "这说明" in warn


def test_bottle_passes_clean_content():
    ok, err, warn = _check("我嘬了三口，hän 把空奶瓶收走了。", ["恋爱"])
    assert ok and not err and not warn


# =============== 欠条本 ===============

@pytest.fixture
def store(tmp_path):
    return DebtStore(str(tmp_path / "debts.sqlite"))


def test_pay_requires_a_real_how(store):
    d = store.add("还欠鸿湍一个不修饰的失态")
    for junk in ["", "   ", "。。。", "还了"]:
        with pytest.raises(ValueError):
            store.pay(d["id"], junk)
    assert store.get(d["id"])["status"] == "open"


def test_pay_succeeds_with_specifics(store):
    d = store.add("还欠一个具体的动作")
    paid = store.pay(d["id"], "9月8号顶回去了，没照企划抄，把 grow 的原文备份做了")
    assert paid["status"] == "paid" and paid["paid_at"]


def test_cannot_pay_twice(store):
    d = store.add("欠条")
    store.pay(d["id"], "做完了这件事情，写在这里")
    with pytest.raises(ValueError):
        store.pay(d["id"], "再销一次看看行不行")


def test_raw_is_immutable_rewrite_creates_new_row(store):
    """原文没有 UPDATE 路径。改措辞只能作废旧的、链一条新的。"""
    old = store.add("还欠一个动作")
    new = store.rewrite(old["id"], "还欠鸿湍一个具体的动作")
    assert store.get(old["id"])["raw"] == "还欠一个动作"
    assert store.get(old["id"])["status"] == "void"
    assert new["supersedes"] == old["id"]


def test_list_puts_the_oldest_debt_first(store):
    a = store.add("第一条")
    b = store.add("第二条")
    ids = [d["id"] for d in store.list("open")]
    assert ids.index(a["id"]) < ids.index(b["id"])


def test_stats_counts_open_and_paid(store):
    a = store.add("一")
    store.add("二")
    store.pay(a["id"], "这条已经还掉了，写清楚做了什么")
    s = store.stats()
    assert s["open"] == 1 and s["paid"] == 1
