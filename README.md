# 应收回款预测台

本项目用于应收回款预测台的业务资料核对与分析留存。目前提供两类分析：`open_balance` 覆盖核对与 `collection_forecast` 八周回款分布预测，业务定义见 `docs/data-contract.md`。示例数据在本地生成，分析不访问远程业务系统。

## 运行环境
Python 3.11；数值库固定在 `requirements.txt`。Dockerfile 通过不可变镜像摘要锁定已验证的 Python 3.11.14，代码兼容控制台 Python 3.11.2。依赖安装属于环境准备，不属于服务启动；已经准备过的环境不再联网下载。

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -m app demo
python3 -m app forecast
python3 -m app serve
```

服务默认监听本机 8080，`GET /health` 检查存活，`GET /v1/catalog` 查看当前分析类型。`POST /v1/analyses` 接收 `request_id`、`operation`、`as_of` 和可选 `options` 对象，返回保存后的分析；`GET /v1/analyses/{request_id}` 打开历史记录。数据文件由 `--source` 指定，SQLite 由 `--database` 指定，参数放在子命令前。`POST /v1/actions` 调用领域操作，已支持的动作见领域模块；没有配置动作的项目会明确拒绝。

## 回款分布预测

`collection_forecast` 在 `open_balance` 同一套核销账本上重建截止时点的真正未清金额，按周给出未来八周（可用 `options.weeks` 调整，1-26 周）的期望回款与 P10/P50/P90 区间，并评估整段期间的现金缺口风险。

口径与建模要点（结果中的 `method` 字段同步完整披露）：

- **样本单位是到账事件**：同一 `payment_id` 一次结清多张发票只算一个历史样本，按其核销发票的金额加权到期日计算迟付天数；每条 allocation 不是独立样本，`reversed` 与高 revision 已在对账层剔除。
- **未清金额逐分守恒**：`reconciliation.forecast_distributes_all_open` 校验八周期望（周内 + 期外 + 不可收）与 `open_balance` 的 `open_cents` 完全相等，取整残差并入第一周。
- **小样本收缩与低把握披露**：客户延误均值按 `n/(n+3)` 向组合均值收缩；没有任何结清记录的客户不会被静默丢掉，而是采用组合先验并标记 `confidence=low`、`has_settlement_history=false`。
- **承诺不是现金**：承诺永不进入实收金额；到期且窗口内没有足够未分摊到账的承诺按未兑现处理，平滑兑现率同时写入结果。由于数据缺少承诺与到账的核销关联，结果明确标注该指标 `verifiable=false`。
- **共同延误的组合风险**：蒙特卡洛（默认 2000 条路径，固定种子可复算）让所有客户共享一个市场冲击因子，`weekly_bands_cents` 为相关区间；另给 `independent_*` 独立假设对照，独立区间系统性偏窄。`gap_at_risk_cents` 给出整段期间现金缺口（期外迟付 + 不可收，同路径联合计算并含 P10/P50）的分位数，其两个分量 `gap_tail_cents`、`gap_uncollectible_cents` 之和严格等于总缺口，不能用各发票独立区间相加代替组合区间。
- **情景试算**：`options.scenarios` 接收按客户的 `delay_days`（追加延误）与 `add_nonpay`（追加拖欠概率）覆盖，基线与每个情景并列返回，金额总量保持不变只改变时间与风险分布。

情景请求示例：

```sh
python3 -m app forecast --options '{"weeks":8,"paths":2000,"scenarios":[
  {"name":"重点客户延迟两周","clients":{"c3":{"delay_days":14}}},
  {"name":"承诺落空","clients":{"c8":{"add_nonpay":0.3}}}]}'
```

## 目录职责
`app/ledger.py` 重建核销账本与未清余额（核对与预测共用），`app/domain.py` 登记业务分析与动作，`app/forecast.py` 实现回款分布预测，`app/contracts.py` 处理可见版本，`app/repository.py` 维护输入与分析记录，`app/service.py` 协调操作和留存，`app/httpd.py` 提供本地接口。`tests` 覆盖业务口径、重复请求、并发回放及真实 HTTP 往返。

## 快速查看
`python3 -m app demo` 从本地数据执行当前覆盖核对并完整打印结果，一般数秒内完成。清理 `var` 可从干净状态重新运行，历史正式记录应另外留存。该命令展示的是当前已具备的核对能力，不代表已完成其他分析。

## 容器与离线复现
首次在运行前执行 `docker build -t local-analysis .` 准备锁定依赖。
之后 `docker run --rm --network none local-analysis python3 -m unittest discover -s tests -v` 检查领域契约和真实 HTTP 工作流，`docker run --rm --network none local-analysis` 从全新容器输出当前业务核对。
容器不访问业务网络，不需要外部数据库或第二个服务；`requirements.lock` 与正式依赖入口 `requirements.txt` 保留相同精确版本。
当前测试和演示的执行上限各为80秒；新增分析应提供90秒以内可实际展示完整结果的入口，依赖准备在演示前完成。
