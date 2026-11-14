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
