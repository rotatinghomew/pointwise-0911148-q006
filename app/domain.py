from collections import Counter
from pathlib import Path

from app.contracts import latest, load
from app.repository import digest

TABLES = ['invoices', 'cash_events', 'allocations', 'promises']

def open_balance(snapshot, options):
    invoices = {row['invoice_id']: row for row in snapshot['invoices']}
    payments = {row['payment_id']: row for row in snapshot['cash_events']}
    active = [row for row in latest(snapshot['allocations'], ['allocation_id']) if row['status'] == 'active']
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
                 'open_cents': row['gross_cents'] - by_invoice[identity]} for identity, row in sorted(invoices.items())]
    gross = sum(row['gross_cents'] for row in invoices.values())
    return {'invoices': len(invoices), 'active_allocations': len(active), 'cash_events': len(payments),
            'gross_cents': gross, 'allocated_cents': sum(by_invoice.values()),
            'open_cents': sum(row['open_cents'] for row in balances), 'balances': balances,
            'clients': len({row['client'] for row in invoices.values()})}

def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')

OPERATIONS = {'open_balance': open_balance}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
