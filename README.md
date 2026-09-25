# 实训设备协作基础服务

本项目提供职业院校、实训场所、设备管理员与设备资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在此之上，`equipment.py` 实现实训设备预约与维护封锁服务：登记设备能力版本、可组合附件、校准证书、开放窗口和转换规则；申请人提交训练目标后获得候选时段与无法满足的原因；确认预约时核对设备与附件版本并原子占用整组资源，缺一项即整体失败；维护人员可发布计划封锁，紧急停用会把未来预约送入改期队列、对已开始的使用生成待人工决定的风险记录，每次人工覆盖都会留痕。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、设备预约服务、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance
PYTHONPATH=src python3 -m skills_workspace.equipment_acceptance
```

第一条命令验收机构、操作者、场所和领域资料登记链；第二条命令在临时 SQLite 数据库中登记轨道车辆台架、无人机装调套件、口腔加工设备与附件，完成候选时段搜索、幂等确认、计划封锁、紧急停用、风险人工决定和改期闭环，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持机构、操作者、场所和领域资料登记，以及审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。

### 设备预约与维护封锁接口

登记类（admin/operator）：

- `POST /equipment`、`POST /equipment-capability-changes`：登记设备能力版本，变更能力会提升版本号；
- `POST /attachments`、`POST /attachment-capability-changes`：登记可组合附件及其兼容设备；
- `POST /calibration-certificates`：登记设备或附件的校准证书（含生效与到期时间）；
- `POST /open-windows`：登记资源开放窗口；
- `POST /changeover-rules`：登记两次训练之间的转换准备分钟数，支持 `*` 通配。

预约类（admin/operator）：

- `POST /slot-searches`：按训练目标、所需能力、所需附件和时长搜索候选时段，返回每个候选的版本、校准依据（证书编号）以及每台设备无法满足的原因；
- `POST /reservations`：确认预约，核对设备与附件版本后在同一事务内原子占用整组资源，任一项不满足即整体失败；携带 `reschedule_entry_id` 可完成改期闭环；
- `POST /reservation-cancellations`：取消预约；
- `GET /reservations/{id}`：查看预约状态、占用版本和校准依据。

维护类（admin/maintainer）：

- `POST /maintenance-blocks`：发布计划封锁，与已确认预约重叠时返回冲突；
- `POST /emergency-deactivations`：紧急停用设备或附件，未来预约进入改期队列，已开始的使用只生成风险记录等待人工决定；
- `POST /reactivations`：恢复停用资源；
- `POST /usage-risk-decisions`：对风险记录做出 `continue` 或 `terminate` 决定；
- `POST /reschedule-entry-closures`：人工关闭改期条目并取消对应预约。

展示类：

- `GET /equipment?site_id=`：设备与附件的能力版本和状态；
- `GET /occupancy?site_id=&start_at=&end_at=`：资源占用（预约与封锁）；
- `GET /reschedule-queue?site_id=`：改期队列与优先级（按原训练开始时间排序）；
- `GET /usage-risks?site_id=`：待人工决定的风险记录；
- `GET /manual-overrides?site_id=`：每次人工覆盖的决定、理由和操作者。

所有写入接口都遵循同一幂等约定：相同 `request_id` 与相同内容安全重放并返回原回执，相同 `request_id` 携带不同内容会返回 `409` 冲突。
