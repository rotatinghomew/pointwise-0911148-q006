from collections import Counter

from app.contracts import latest


def reconcile(snapshot):
    """按截止快照重建真正未清余额，返回核对与预测共用的对账事实。

    同一 allocation_id 只取最高 revision，status=reversed 的分摊释放；
    同一 payment_id 的多条分摊合并视为一笔到账，绝不把每条核销当独立样本。
    """
    invoices = {row['invoice_id']: row for row in snapshot['invoices']}
    payments = {row['payment_id']: row for row in snapshot['cash_events']}
    active = [row for row in latest(snapshot['allocations'], ['allocation_id'])
              if row['status'] == 'active']
    by_invoice = Counter()
    by_payment = Counter()
    for row in active:
        if row['invoice_id'] not in invoices or row['payment_id'] not in payments:
            raise ValueError('核销引用不存在')
        if row['amount_cents'] < 0 or invoices[row['invoice_id']]['client'] != payments[row['payment_id']]['client']:
            raise ValueError('核销客户或金额无效')
        by_invoice[row['invoice_id']] += row['amount_cents']
        by_payment[row['payment_id']] += row['amount_cents']
    if any(amount > invoices[identity]['gross_cents'] for identity, amount in by_invoice.items()):
        raise ValueError('分摊超过发票金额')
    if any(amount > payments[identity]['amount_cents'] for identity, amount in by_payment.items()):
        raise ValueError('分摊超过实际到账')
    balances = [{'invoice_id': identity, 'client': row['client'], 'due_at': row['due_at'],
                 'gross_cents': row['gross_cents'],
                 'open_cents': row['gross_cents'] - by_invoice[identity]}
                for identity, row in sorted(invoices.items())]
    return {
        'invoices': invoices,
        'payments': payments,
        'active_allocations': active,
        'allocated_by_invoice': by_invoice,
        'allocated_by_payment': by_payment,
        'balances': balances,
    }
