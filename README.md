# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 跨境付款责任链

`civicflow.payments` 与 `civicflow.dayend` 在平台基元之上组合出围绕月末关账与差错处理的跨境付款责任链：

- **提交即固化**：指令提交时冻结合同验收状态、收付款双方资料（含摘要）和业务水位，按币种与费用形成付款组成；同一业务标识无论重放还是并发提交只得到一笔有效指令。
- **事件归并**：桥侧受理、清算、结算、退回、撤销、退款事件经收件箱按来源序号归并到同一经济事项；缺号等待、重放去重、内容矛盾记录差错。
- **核对队列**：金额、币种或收款方与既有记录矛盾时进入核对队列，绝不自动补付；人工确认后才可维持原记录或以来电为准补记差额分录。
- **职责分离**：提交者不能批准自己的指令；资料变化或合规命中会暂停后续处理，合规岗位解除暂停时以当前资料重新固化并留痕。
- **结算后只留新分录**：已结算款项只能通过桥侧退款（支持部分退款）或冲正留下新的分录，历史分录不可变。
- **日终恢复**：日终关账由检查点驱动的五个幂等步骤组成，中途停机后任务租约过期被重新认领，从未完成的步骤继续。
- **统一查询**：`status` 一次返回业务进度、资金落点、当前责任人、差错来源和任一历史时点余额；`side:payer` / `side:payee` 范围把双方岗位可见字段限制在履职范围内。

岗位权限：`write:payments`、`approve:payments`、`dispatch:payments`、`ingest:bridge`、`release:payments`、`resolve:reconciliation`、`correct:payments`、`read:payments`、`history:payments`、`write:profiles`、`run:dayend`。

运行跨境付款演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-pay.sqlite3 payment-demo
```

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```
