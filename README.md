# 实训设备协作基础服务

本项目提供职业院校、实训场所、设备管理员与设备资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在基础能力之上，`BookingService` 提供实训设备预约与维护封锁：登记设备能力版本、可组合附件、校准证书、开放窗口和两次训练之间的转换规则；申请人提交训练目标后获得按分钟扫描的候选时段以及无法满足的明确原因。确认预约时核对设备与附件的能力版本，并通过分钟槽唯一约束原子占用整组资源——任一设备或附件冲突，整组回滚，不会部分成功。维护人员可发布计划封锁（可凭人工豁免占用）或紧急停用（不可豁免），紧急停用会把未来预约移入按优先级排序的改期队列，已经开始的使用只登记风险等待人工决定。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、设备预约与维护封锁、HTTP 路由和离线验收；
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
```

命令会在临时 SQLite 数据库中登记机构、操作者、场所和领域资料，并完成设备登记、候选查询、原子确认、紧急停用、改期队列与重新预约的完整链路，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## 设备预约接口

所有写入接口通过 `X-Actor-Id` 标识操作者，且必须带 `request_id`；相同请求安全重放返回原回执，不同内容复用编号返回 `409 conflict`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/booking/equipment` | 登记设备能力与能力版本 |
| POST | `/booking/accessories` | 登记可组合附件（可带兼容设备） |
| POST | `/booking/compatibility` | 增补设备-附件组合关系 |
| POST | `/booking/certificates` | 登记设备或附件的校准证书与到期时间 |
| POST | `/booking/open-windows` | 登记设备开放窗口 |
| POST | `/booking/transition-rules` | 登记两种 setup 之间的转换准备分钟数 |
| POST | `/booking/inquiries` | 提交训练目标，返回候选时段与无法满足原因 |
| GET | `/booking/inquiry?inquiry_id=` | 查看咨询及其候选快照 |
| POST | `/booking/reservations` | 核对版本后原子占用整组资源 |
| POST | `/booking/blockades` | 发布计划封锁或紧急停用 |
| POST | `/booking/blockades/lift` | 人工提前解除封锁（记录覆盖） |
| GET | `/booking/reschedule-queue?site_id=` | 查看改期队列与优先级位置 |
| POST | `/booking/reschedule/rebook` | 把队列条目原子改到新时段 |
| POST | `/booking/reschedule/cancel` | 放弃队列条目 |
| GET | `/booking/usage-risks?site_id=` | 查看进行中使用遇到封锁的风险 |
| POST | `/booking/usage-risks/resolve` | 人工决定继续、终止或改期 |
| GET | `/booking/equipment?site_id=` | 查看设备、附件、版本、证书与窗口 |
| GET | `/booking/reservations?site_id=` | 查看预约、版本、校准依据与人工覆盖 |
| GET | `/booking/manual-overrides?site_id=` | 查看每次人工覆盖 |
| GET | `/booking/occupancy?resource_type=&resource_id=` | 查看资源占用与封锁时间线 |

候选时段中 `violations` 为空表示可直接确认；`waivers_required` 非空时（计划封锁、转换准备、校准到期），确认时必须在 `waivers` 中逐项给出理由，每次豁免都会写入人工覆盖。占用冲突和紧急停用不可豁免。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持机构、操作者、场所、领域资料登记，以及设备预约与维护封锁全套接口和审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。
