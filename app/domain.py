from pathlib import Path

from app.contracts import load
from app.forecast import collection_forecast as run_forecast
from app.ledger import reconcile

TABLES = ['invoices', 'cash_events', 'allocations', 'promises']


def open_balance(snapshot, options, as_of=None):
    facts = reconcile(snapshot)
    gross = sum(row['gross_cents'] for row in facts['invoices'].values())
    balances = [{key: row[key] for key in ('invoice_id', 'client', 'due_at', 'open_cents')}
                for row in facts['balances']]
    return {'invoices': len(facts['invoices']),
            'active_allocations': len(facts['active_allocations']),
            'cash_events': len(facts['payments']),
            'gross_cents': gross,
            'allocated_cents': sum(facts['allocated_by_invoice'].values()),
            'open_cents': sum(row['open_cents'] for row in balances),
            'balances': balances,
            'clients': len({row['client'] for row in facts['invoices'].values()})}


def collection_forecast(snapshot, options, as_of=None):
    return run_forecast(snapshot, options or {}, as_of)


def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')


OPERATIONS = {'open_balance': open_balance, 'collection_forecast': collection_forecast}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
