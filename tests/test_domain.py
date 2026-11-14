import copy

import unittest
from app.domain import open_balance, seed_dataset

class DomainCoverageTests(unittest.TestCase):
    def test_coverage_reports_domain_entities(self):
        result = open_balance(seed_dataset(), {})
        self.assertTrue(result)
        self.assertGreaterEqual(len(result), 3)

    def test_empty_records_are_reported_without_crash(self):
        dataset = seed_dataset()
        for records in dataset.values():
            records.clear()
        result = open_balance(dataset, {})
        self.assertIsInstance(result, dict)

    def test_source_data_is_not_mutated(self):
        dataset = seed_dataset()
        before = copy.deepcopy(dataset)
        open_balance(dataset, {})
        self.assertEqual(dataset, before)
