# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## quarantine 架构：B（舱内处置）

三联差额被标为 quarantine 后进入暂存舱（`quarantine_staging`，held 状态），全局只贯彻以下口径：

- 只能在舱内处置：认短/认溢/退回待处置/丢弃（discard）；**不提供出舱回灌装载旁路**。
- `GET /api/reconcile/quarantine/export` 显式返回 409 只读说明；回灌路由不存在（404）。
- held 键与正式命运互斥：同批同键只允许一条 held（部分唯一索引 `ux_quarantine_held_key` 兜底），同键双态拒绝。
- 结案闸：held 未清或 pending 未清均禁止 sealed；seal 用条件 UPDATE 守卫，全部写操作在 `BEGIN IMMEDIATE` 单事务内完成，无半截升级。
- 匹配闭包 `/reconcile/matches` 只返回 aligned=1 且非 held 的键；quarantine 键不进 matched、不进 sealed 对照。
- 计数联动：批次快照 `quarantine_count`、总览暂存计数、暂存列表（`/reconcile/staging`）三方同口径。
- 命运变更全程留痕 `variance_fate_audit`；清舱行保留为 cleared，不物理抹除。
- sealed 后不可改命运、不可写三线（gate）、不可重跑对账，旧批无法补 quarantine 键。

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```
