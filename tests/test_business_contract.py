import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.contracts import visible
from app.domain import OPERATIONS, seed_dataset
from app.repository import digest
from app.service import Service


class BusinessContractTests(unittest.TestCase):
    def setUp(self):
        self.data = seed_dataset()
        self.operation = next(iter(OPERATIONS))

    def run_check(self):
        return OPERATIONS[self.operation](self.data, {})

    def test_balances_conserve_amounts_after_reversal(self):
        result = self.run_check()
        self.assertEqual(result['gross_cents'], result['open_cents'] + result['allocated_cents'])
        self.assertEqual(result['open_cents'], sum(row['open_cents'] for row in result['balances']))
        self.assertEqual(len(self.data['cash_events']), len({row['payment_id'] for row in self.data['cash_events']}))
        self.assertLess(result['active_allocations'], len(self.data['allocations']))

    def test_excess_allocation_is_not_silently_clipped(self):
        self.data['allocations'][1]['amount_cents'] = 10**12
        with self.assertRaises(ValueError):
            self.run_check()


    def test_fixture_timestamps_do_not_arrive_before_events(self):
        from app.contracts import instant
        for records in self.data.values():
            for row in records:
                if 'received_at' not in row:
                    continue
                for key in ('event_at', 'assigned_at', 'signed_at', 'paid_at', 'issued_at'):
                    if row.get(key):
                        self.assertLessEqual(instant(row[key]), instant(row['received_at']))

    def test_seed_and_live_source_have_one_contract(self):
        from app.contracts import load
        self.assertEqual(self.data, load('data/source.json'))
