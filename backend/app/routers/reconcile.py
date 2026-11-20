from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import reconcile_service as svc

router = APIRouter(tags=["reconcile"])


class RunIn(BaseModel):
    dock_day: str


class SealIn(BaseModel):
    dock_day: str


class FateIn(BaseModel):
    fate: str
    note: str | None = None


# 架构 B（舱内处置）下明确不提供的旁路。回灌装载连路由都不存在；
# 导出报文以显式 409 说明不可用，避免调用方误以为是遗漏。
_EXPORT_DISABLED_DETAIL = (
    "当前为舱内处置架构：quarantine 差额不得出舱回灌，导出报文接口不可用。"
    "只读暂存清单见 GET /reconcile/staging；如需改命运请在舱内处置或丢弃。"
)


@router.post("/reconcile/run")
def run_reconcile(body: RunIn):
    conn = connect()
    try:
        return svc.run_reconcile(conn, body.dock_day)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@router.post("/reconcile/seal")
def seal(body: SealIn):
    conn = connect()
    try:
        return svc.seal_day(conn, body.dock_day)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@router.get("/reconcile/batches")
def batches(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            rows = conn.execute(
                "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reconcile_batches ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/reconcile/matches")
def matches(batch_id: int):
    """匹配闭包：只返回对齐键。held 暂存键（aligned=0 且在舱）被过滤，不进 matched。"""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT m.* FROM match_rows m
               WHERE m.batch_id=? AND m.aligned=1
                 AND NOT EXISTS (
                   SELECT 1 FROM quarantine_staging q
                   WHERE q.batch_id=m.batch_id AND q.supplier_code=m.supplier_code
                     AND q.sku=m.sku AND q.status='held'
                 )
               ORDER BY m.supplier_code, m.sku""",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/reconcile/variances")
def variances(batch_id: int | None = None, dock_day: str | None = None):
    conn = connect()
    try:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        elif dock_day:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/reconcile/staging")
def staging(batch_id: int | None = None, dock_day: str | None = None,
            status: str = "held"):
    """暂存舱列表（默认只看在舱 held）。只读。"""
    conn = connect()
    try:
        return svc.list_staging(conn, batch_id=batch_id, dock_day=dock_day, status=status)
    finally:
        conn.close()


@router.get("/reconcile/audit")
def audit(batch_id: int | None = None, variance_id: int | None = None):
    """命运审计留痕。"""
    conn = connect()
    try:
        return svc.list_audit(conn, batch_id=batch_id, variance_id=variance_id)
    finally:
        conn.close()


@router.get("/reconcile/quarantine/export")
def quarantine_export():
    raise HTTPException(409, _EXPORT_DISABLED_DETAIL)


@router.post("/reconcile/variances/{variance_id}/fate")
def set_fate(variance_id: int, body: FateIn):
    conn = connect()
    try:
        return svc.set_variance_fate(conn, variance_id, body.fate, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()
