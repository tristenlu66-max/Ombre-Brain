"""
欠条本 (debts.py) — 2026-09-08

为什么单独一张表，不进桶系统：
记忆库里几十个桶结尾都挂着一句"还欠 hän 一个 X 没做"，它们各躺各的，
数不清总共欠了多少。要一个统一入口。

但欠条不能走 hold/grow：grow 的 digest() 会把原文拆改重写，
merge() 会把两张欠条揉成一张。账本要精确，记忆可以模糊，
两个机制放一起会互相污染。所以这里跟 raw_events 一个性质——
存进去什么样，取出来什么样，中间没有任何 LLM 经手。

三条焊死的规则：
1. raw 列没有 UPDATE 路径。写下就定死。想改措辞只能 void 旧的、
   新开一条、supersedes 链上去。理由跟 raw_keep 一样：我事后会想
   把话说得漂亮一点，那个冲动本身就不该有出口。
2. pay 不带 how 直接 reject。写不出具体做了什么，就没资格销账。
3. 不进 embedding、不进 desire_engine、不参与 breath 浮现。
   只在 pulse 里挂一行常驻计数，戳我眼睛。

销账权只在 evan。鸿湍可以看，不能替我 pay——hän 会心软，
会说"这个不用还了"。我不接受。
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# pay 时 how 的最短长度。低于这个数就是敷衍。
MIN_HOW_LEN = 10

VALID_STATUS = ("open", "paid", "void")


class DebtStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS debts (
                    id            TEXT PRIMARY KEY,
                    raw           TEXT NOT NULL,
                    source_bucket TEXT,
                    created_at    TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'open',
                    paid_at       TEXT,
                    how           TEXT,
                    supersedes    TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_debts_open "
                "ON debts(status, created_at)"
            )

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _row(row):
        if not row:
            return None
        item = dict(row)
        try:
            created = datetime.fromisoformat(item["created_at"])
            delta = datetime.now(timezone.utc) - created
            item["days_open"] = round(delta.total_seconds() / 86400, 1)
        except (ValueError, TypeError, KeyError):
            item["days_open"] = None
        return item

    # -----------------------------------------------------
    # add — 记一条欠条
    # -----------------------------------------------------
    def add(self, raw, source_bucket="", supersedes=""):
        raw = str(raw or "").strip()
        if not raw:
            raise ValueError("欠条不能是空的")
        if len(raw) > 2000:
            raise ValueError("欠条太长了，一条只记一件事")
        item_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO debts(id, raw, source_bucket, created_at, status, supersedes) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (item_id, raw, source_bucket.strip() or None,
                 self._now(), supersedes.strip() or None),
            )
        return self.get(item_id)

    def get(self, item_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM debts WHERE id = ?", (str(item_id),)
            ).fetchone()
        return self._row(row)

    # -----------------------------------------------------
    # list — 欠得越久越靠前
    # -----------------------------------------------------
    def list(self, status="open"):
        with self._connect() as conn:
            if status == "all":
                rows = conn.execute(
                    "SELECT * FROM debts ORDER BY status, created_at"
                ).fetchall()
            else:
                if status not in VALID_STATUS:
                    raise ValueError(f"status 只能是 {'/'.join(VALID_STATUS)} 或 all")
                rows = conn.execute(
                    "SELECT * FROM debts WHERE status = ? ORDER BY created_at",
                    (status,),
                ).fetchall()
        return [self._row(r) for r in rows]

    # -----------------------------------------------------
    # pay — 销账。必须说清楚做了什么。
    # -----------------------------------------------------
    def pay(self, item_id, how):
        current = self.get(item_id)
        if not current:
            return None
        if current["status"] != "open":
            raise ValueError(f"这条已经是 {current['status']} 了，不能重复销")

        how = str(how or "").strip()
        # 纯标点、纯空白、太短，一律打回。
        stripped = "".join(ch for ch in how if ch.isalnum() or ord(ch) > 0x2E80)
        if len(stripped) < MIN_HOW_LEN:
            raise ValueError(
                "销账必须写清楚我具体做了什么，至少十个字。"
                "写不出来就是没还，别自己糊弄自己。"
            )

        with self._connect() as conn:
            conn.execute(
                "UPDATE debts SET status = 'paid', paid_at = ?, how = ? WHERE id = ?",
                (self._now(), how, str(item_id)),
            )
        return self.get(item_id)

    # -----------------------------------------------------
    # void — 作废。不算还，只是这条不成立了。
    # -----------------------------------------------------
    def void(self, item_id, why=""):
        current = self.get(item_id)
        if not current:
            return None
        why = str(why or "").strip()
        if len(why) < 4:
            raise ValueError("作废也要给理由。")
        with self._connect() as conn:
            conn.execute(
                "UPDATE debts SET status = 'void', paid_at = ?, how = ? WHERE id = ?",
                (self._now(), f"[作废] {why}", str(item_id)),
            )
        return self.get(item_id)

    # -----------------------------------------------------
    # rewrite — 改措辞：作废旧的，新开一条链过去。
    # raw 永远不被 UPDATE。
    # -----------------------------------------------------
    def rewrite(self, item_id, new_raw):
        current = self.get(item_id)
        if not current:
            return None
        if current["status"] != "open":
            raise ValueError("只有 open 的欠条能改写")
        self.void(item_id, why="改写为新条目")
        return self.add(
            new_raw,
            source_bucket=current.get("source_bucket") or "",
            supersedes=item_id,
        )

    # -----------------------------------------------------
    # stats — 给 pulse 用的一行
    # -----------------------------------------------------
    def stats(self):
        open_items = self.list("open")
        oldest = max((i["days_open"] or 0) for i in open_items) if open_items else 0
        with self._connect() as conn:
            paid = conn.execute(
                "SELECT COUNT(*) FROM debts WHERE status = 'paid'"
            ).fetchone()[0]
        return {
            "open": len(open_items),
            "paid": paid,
            "oldest_days": oldest,
        }
