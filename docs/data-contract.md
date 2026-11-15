# 业务记录约定

invoices 的 gross_cents 为整数分，issued_at、due_at 表示出单和到期。cash_events 以 payment_id 标识一笔实际到账，amount_cents 可被分摊到多张发票。allocations 按 allocation_id 记录最新 revision，status=reversed 的分摊不再生效。
promises 以 promise_id 和 revision 标识客户预计支付日期和金额的陈述，且可能撤回；它不是现金到账。所有记录带 received_at，只能按截至可知记录解释未清余额。无回款历史的未结发票不能从训练中静默丢掉。
现有 open_balance 验证生效分摊不超过到账和发票金额，输出发票未清金额与到期日，不预测未来。周预测边界从截止次日起按七天计。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


同一 payment_id 只出现一条 cash_events 到账记录；多张发票的 allocation 可以共同引用该到账。promises 的稳定身份为 promise_id，较高 revision 替换旧承诺版本。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。


## 已有能力与字段边界

payment_id 对应唯一到账，多个 allocation_id 可以引用它；同一 allocation_id 的较高 revision 替换旧版本，reversed 释放原分摊。promise_id 是承诺修订身份，withdrawn 不再有效。open_balance 当前验证金额守恒并给发票未清余额、客户及到期日，不把承诺当现金，不计算未来分布或组合风险。

## collection_forecast 预测口径

collection_forecast 与 open_balance 共用 app/ledger.py 的核销重建结果：同一 allocation_id 只取最高 revision、reversed 释放后，按真正未清金额（gross 减生效分摊）建模，预测分配总额与 open_balance 的 open_cents 逐分相等，结果内 reconciliation.forecast_distributes_all_open 给出校验。预测只使用 as_of 可见记录，周边界从截止次日起按七天计，默认八周。

历史行为的样本单位是 payment_id 到账事件而非 allocation 行：一笔到账分摊到多张发票只产生一个迟付样本，迟付天数为到账日相对该笔到账所核销发票按金额加权到期日的差。客户样本不足时，延误均值与波动按 n/(n+3) 向组合均值收缩；没有任何结清记录的客户保留在输出中，采用组合先验并标记 has_settlement_history=false、confidence=low，不静默丢弃。把握度分档为 settlement_events 0=low、1-3=low、4-7=medium、8 及以上=high。

承诺在任何模式下都不计入实收金额，也不改变预测分配的总额。到期承诺仅当窗口内存在足以覆盖承诺金额的未分摊到账才视为兑现；因数据没有承诺与到账的核销关联字段，该判断不可独立验证，结果 promises.verifiable 固定为 false 并附说明，无法验证的成熟承诺按未兑现处理，每个未兑现承诺在基础先验拖欠率 2% 上叠加 5 个百分点（封顶 60%）。promise_mode 支持 default、ignore（忽略全部承诺信号）、anchor（仅把未来承诺日作为时间均值的部分锚点）。

每周期望金额来自以发票预期滞后为均值、客户波动为标准差的正态分布按周切分的概率质量；期外 tail 与不可收 uncollectible 单独列示。蒙特卡洛默认 2000 条路径、固定随机种子，所有客户共享 N(0, market_sigma) 市场冲击以反映共同延误，weekly_bands_cents 为相关结果，independent_* 字段为去除共同因子的独立假设对照，独立区间偏窄，禁止用各发票独立区间相加代替组合区间。gap_at_risk_cents 在同一条模拟路径上把缺口分解为 gap_tail_cents（八周期外迟付）与 gap_uncollectible_cents（不可收），两个分量分位数之和严格等于总缺口分位数，并同时给出 P10/P50。情景试算经 options.scenarios 传入按客户的 delay_days 与 add_nonpay 覆盖，基线与情景的未清总额保持一致，只改变时间分布与风险。
