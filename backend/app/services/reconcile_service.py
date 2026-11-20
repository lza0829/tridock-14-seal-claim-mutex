"""三联对账领域服务。

全局贯彻架构 B（舱内处置）：
- quarantine 差额只能在舱内改命运（认短/认溢/退回待处置）或丢弃（discard）；
- 不提供回灌装载旁路，出舱导出接口显式不可用（见 routers/reconcile.py）；
- 暂存 held 行与 variance_rows 正式命运互斥，同批同键只允许一条 held（DB 部分唯一索引兜底）；
- 结案前 quarantine held 键不得计入 matched、不得进 sealed 对照；
- 所有命运变更写 variance_fate_audit；批次快照带 quarantine_count；
- 全部写操作在单事务 BEGIN IMMEDIATE 内完成，杜绝并发半截升级。
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

# 正式命运（variance_rows.fate）
FATE_PENDING = "pending"
FATE_ACCEPT_SHORT = "accept_short"
FATE_ACCEPT_OVER = "accept_over"
FATE_QUARANTINE = "quarantine"
FATE_DISCARDED = "discarded"
FORMAL_FATES = {
    FATE_PENDING,
    FATE_ACCEPT_SHORT,
    FATE_ACCEPT_OVER,
    FATE_QUARANTINE,
    FATE_DISCARDED,
}
# 舱内允许的处置：不允许通过本接口直接传 staging 之外的旁路值。
CABIN_FATES = {
    FATE_PENDING,
    FATE_ACCEPT_SHORT,
    FATE_ACCEPT_OVER,
    FATE_QUARANTINE,
    FATE_DISCARDED,
}

STAGING_HELD = "held"
STAGING_CLEARED = "cleared"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _tx(conn: sqlite3.Connection):
    """单连接串行写事务：BEGIN IMMEDIATE 立即拿写锁，提交/回滚原子化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _sum_map(conn: sqlite3.Connection, table: str, dock_day: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}


def _audit(conn, variance_id: int, batch_id: int, dock_day: str,
           supplier_code: str, sku: str, from_fate: str | None, to_fate: str,
           note: str | None = None) -> None:
    conn.execute(
        '''INSERT INTO variance_fate_audit
           (variance_id, batch_id, dock_day, supplier_code, sku, from_fate, to_fate, changed_at, note)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (variance_id, batch_id, dock_day, supplier_code, sku, from_fate, to_fate, _now(), note),
    )


def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    with _tx(conn):
        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
            (dock_day,),
        ).fetchone()
        if sealed:
            raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账")

        # 仅清理同到货日的 open 批次；sealed 批次（含其暂存/审计）永不触碰，
        # 因此 sealed 后不可能把 quarantine 键补进旧批。
        old = conn.execute(
            "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open'",
            (dock_day,),
        ).fetchall()
        for r in old:
            conn.execute("DELETE FROM variance_fate_audit WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM quarantine_staging WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM match_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM reconcile_batches WHERE id=?", (r["id"],))

        expect_m = _sum_map(conn, "expect_lines", dock_day)
        receive_m = _sum_map(conn, "receive_lines", dock_day)
        confirm_m = _sum_map(conn, "confirm_lines", dock_day)
        keys = set(expect_m) | set(receive_m) | set(confirm_m)

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        cur = conn.execute(
            '''INSERT INTO reconcile_batches
               (batch_no, dock_day, matched_count, variance_count, quarantine_count, status, sealed_at, note)
               VALUES (?, ?, 0, 0, 0, 'open', NULL, ?)''',
            (batch_no, dock_day, "三联对齐"),
        )
        batch_id = int(cur.lastrowid)

        matched = 0
        variance = 0
        now = _now()
        for supplier_code, sku in sorted(keys):
            eq = expect_m.get((supplier_code, sku), 0.0)
            rq = receive_m.get((supplier_code, sku), 0.0)
            cq = confirm_m.get((supplier_code, sku), 0.0)
            aligned = 1 if eq == rq == cq else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, fate, created_at)
                       VALUES (?,?,?,?,?,?,?, 'pending', ?)''',
                    (batch_id, dock_day, supplier_code, sku, eq, rq, cq, now),
                )

        # 新批次无暂存，quarantine_count 固定 0；计数与行在同一事务内落库。
        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=?, quarantine_count=0 WHERE id=?",
            (matched, variance, batch_id),
        )
    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "dock_day": dock_day,
        "matched_count": matched,
        "variance_count": variance,
        "quarantine_count": 0,
        "status": "open",
    }


def seal_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    with _tx(conn):
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
            (dock_day,),
        ).fetchone()
        if not batch:
            raise ValueError("没有可结案的对账批次，请先跑三联对账")
        bid = batch["id"]

        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (bid,),
        ).fetchone()["c"]
        held = conn.execute(
            "SELECT COUNT(*) c FROM quarantine_staging WHERE batch_id=? AND status='held'",
            (bid,),
        ).fetchone()["c"]
        # 架构 B 结案闸：quarantine 未清（仍在舱）与 pending 一样阻断 sealed。
        if held:
            raise ValueError(f"仍有 {held} 条 quarantine 差额在暂存舱未处置，不能结案")
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")

        # 快照与行的一致性交叉核对，防止计数与列表口径漂移。
        snap = conn.execute(
            "SELECT matched_count, variance_count, quarantine_count FROM reconcile_batches WHERE id=?",
            (bid,),
        ).fetchone()
        real_var = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=?", (bid,)
        ).fetchone()["c"]
        if real_var != snap["variance_count"]:
            raise ValueError("批次差额计数与差额行不一致，拒绝结案")
        if held != snap["quarantine_count"]:
            raise ValueError("批次暂存计数与暂存舱不一致，拒绝结案")

        cur = conn.execute(
            "UPDATE reconcile_batches SET status='sealed', sealed_at=? WHERE id=? AND status='open'",
            (_now(), bid),
        )
        if cur.rowcount != 1:
            # 并发结案/改批次时条件更新落空，绝不半截升级。
            raise ValueError("批次状态已变更，结案失败，请刷新后重试")
        row = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (bid,)).fetchone()
        return dict(row)


def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str,
                      note: str | None = None) -> dict:
    if fate not in CABIN_FATES:
        raise ValueError("非法差额命运")
    with _tx(conn):
        row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
        if not row:
            raise ValueError("差额行不存在")
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)
        ).fetchone()
        if batch and batch["status"] == "sealed":
            raise ValueError("已结案批次不可改差额命运")

        bid = row["batch_id"]
        old_fate = row["fate"]
        if old_fate == fate:
            raise ValueError(f"该差额命运已是 {fate}，无需重复处置")

        held_row = conn.execute(
            "SELECT * FROM quarantine_staging WHERE variance_id=? AND status='held'",
            (variance_id,),
        ).fetchone()

        if fate == FATE_QUARANTINE:
            if held_row:
                raise ValueError("该键已在暂存舱，禁止同键重复入舱（同键双态被拒）")
            # 入舱：fate 置 quarantine 并建 held 暂存行；部分唯一索引兜底同批同键双态。
            try:
                conn.execute(
                    '''INSERT INTO quarantine_staging
                       (batch_id, variance_id, dock_day, supplier_code, sku, reason, status, created_at, cleared_at)
                       VALUES (?,?,?,?,?,?, 'held', ?, NULL)''',
                    (bid, variance_id, row["dock_day"], row["supplier_code"], row["sku"], note, _now()),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError("同键暂存与正式命运并存被拒（暂存键已存在）") from e
        else:
            # 舱内改命运/丢弃：若该键在舱，先清舱再落正式命运；不存在回灌旁路。
            if held_row:
                conn.execute(
                    "UPDATE quarantine_staging SET status='cleared', cleared_at=? WHERE id=?",
                    (_now(), held_row["id"]),
                )

        conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
        _audit(
            conn, variance_id, bid, row["dock_day"], row["supplier_code"], row["sku"],
            old_fate, fate, note,
        )

        held_n = conn.execute(
            "SELECT COUNT(*) c FROM quarantine_staging WHERE batch_id=? AND status='held'",
            (bid,),
        ).fetchone()["c"]
        conn.execute(
            "UPDATE reconcile_batches SET quarantine_count=? WHERE id=?", (held_n, bid)
        )
        return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())


def list_staging(conn: sqlite3.Connection, batch_id: int | None = None,
                 dock_day: str | None = None, status: str = STAGING_HELD) -> list[dict]:
    sql = "SELECT * FROM quarantine_staging WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if batch_id is not None:
        sql += " AND batch_id=?"
        args.append(batch_id)
    elif dock_day:
        sql += " AND dock_day=?"
        args.append(dock_day)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def list_audit(conn: sqlite3.Connection, batch_id: int | None = None,
               variance_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM variance_fate_audit WHERE 1=1"
    args: list = []
    if batch_id is not None:
        sql += " AND batch_id=?"
        args.append(batch_id)
    if variance_id is not None:
        sql += " AND variance_id=?"
        args.append(variance_id)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
