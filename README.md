# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10831 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 冻写切断（本仓已钉死）

冻结徽标仍亮时，冻结源报文不得再入正式到达流水。切断口径与封档见
[`docs/冻写切断卡.md`](docs/冻写切断卡.md)（甲路径：整笔锁行拒插 + 审计 + 正式零增）。

- 入口：沿用「时限台 → 到达登记」菜单，不另建冻写台；登录沿用冻写侧会话。
- 库文件挂冻写专卷：compose 卷 `lagboard-freeze-data:/app/data`。
- 失败测：`tests/test_freeze_write_cut.py`（拒写零增、五处同拒、双席争用留痕、偏离卡驳回）。
- 复现脚本：`python scripts/repro_freeze_ingest.py`。
- 封档：`python scripts/seal_freeze_card.py`（仅钉死当天，改口须 `--force` 并说明）。

相关接口：`POST /api/arrivals`（冻结源返回 423）、`GET /api/freeze-audit`、
`GET /api/tasks`、`GET /api/watermark`。

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10831/  
健康检查：GET http://127.0.0.1:10831/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
