import copy
import unittest

from app.domain import OPERATIONS, collection_forecast, open_balance, seed_dataset

AS_OF = '2026-09-30T23:59:59Z'


def forecast(dataset, **options):
    return collection_forecast(dataset, options or {}, AS_OF)


class ForecastReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.dataset = seed_dataset()

    def test_forecast_distributes_exact_open_balance_per_cent(self):
        coverage = open_balance(self.dataset, {})
        result = forecast(self.dataset)
        self.assertTrue(result['reconciliation']['identity_check'])
        self.assertTrue(result['reconciliation']['forecast_distributes_all_open'])
        baseline = result['scenarios'][0]
        self.assertEqual(coverage['open_cents'], result['reconciliation']['open_cents'])
        self.assertEqual(baseline['expected_total_cents'], coverage['open_cents'])
        # 期望分布逐分守恒：周内 + 期外 + 不可收 = 未清总额
        self.assertEqual(
            sum(baseline['expected_weekly_cents'])
            + baseline['expected_tail_cents']
            + baseline['expected_uncollectible_cents'],
            coverage['open_cents'])
        # 客户层同样逐分守恒，且客户周值可加总为组合周值
        for client in baseline['client_forecasts']:
            self.assertEqual(
                sum(client['expected_weekly_cents'])
                + client['expected_tail_cents']
                + client['expected_uncollectible_cents'],
                client['open_cents'])
        for week in range(8):
            self.assertEqual(
                baseline['expected_weekly_cents'][week],
                sum(client['expected_weekly_cents'][week]
                    for client in baseline['client_forecasts']))

    def test_reversal_and_revision_do_not_double_count(self):
        dataset = copy.deepcopy(self.dataset)
        allocation = next(row for row in dataset['allocations']
                          if row['allocation_id'] == 'x0-1' and row['revision'] == 1)
        allocation['amount_cents'] = 10**9
        # 该修订已被 revision=2 reversed 取代，预测与核对都不应采用其金额
        coverage = open_balance(dataset, {})
        result = forecast(dataset)
        self.assertEqual(result['reconciliation']['open_cents'], coverage['open_cents'])
        self.assertTrue(result['reconciliation']['identity_check'])

    def test_payment_event_is_the_sample_unit(self):
        result = forecast(self.dataset)
        for profile in result['client_behavior']:
            # 30 个客户各自只有 1-2 笔到账，而 allocation 行数更多；
            # 若误按核销行计数，settlement_events 会显著大于到账事件数
            self.assertLessEqual(profile['settlement_events'], 2)
        self.assertEqual(len(result['client_behavior']), 30)


class PromiseTests(unittest.TestCase):
    def setUp(self):
        self.dataset = seed_dataset()

    def test_promises_are_never_counted_as_cash(self):
        result = forecast(self.dataset)
        baseline = result['scenarios'][0]
        coverage = open_balance(self.dataset, {})
        # 承诺不改变预测分配的总额，总额严格等于未清余额
        self.assertEqual(baseline['expected_total_cents'], coverage['open_cents'])
        self.assertEqual(result['promises']['open_promises'], 29)
        # 9 月承诺全部到期且窗口内无未分摊到账，按未兑现处理并如实披露不可验证
        self.assertEqual(result['promises']['matured_broken_promises'], 29)
        self.assertFalse(result['promises']['verifiable'])
        self.assertIsNotNone(result['method']['promise_reliability_smoothed'])
        self.assertLess(result['method']['promise_reliability_smoothed'], 0.1)

    def test_ignore_mode_drops_promise_penalty(self):
        default_result = forecast(self.dataset)
        ignored = forecast(self.dataset, promise_mode='ignore')
        default_nonpay = sum(c['nonpay_probability'] for c in
                             default_result['scenarios'][0]['client_forecasts'])
        ignored_nonpay = sum(c['nonpay_probability'] for c in
                             ignored['scenarios'][0]['client_forecasts'])
        self.assertLess(ignored_nonpay, default_nonpay)
        self.assertEqual(ignored['scenarios'][0]['expected_total_cents'],
                         default_result['scenarios'][0]['expected_total_cents'])

    def test_anchor_mode_shifts_timing_toward_future_promise(self):
        dataset = {
            'invoices': [{'invoice_id': 'i1', 'client': 'c', 'gross_cents': 100000,
                          'issued_at': '2026-09-01', 'due_at': '2026-10-01',
                          'received_at': '2026-09-01T00:00:00Z'}],
            'cash_events': [], 'allocations': [],
            'promises': [{'promise_id': 'pr', 'revision': 1, 'client': 'c',
                          'amount_cents': 50000, 'expected_date': '2026-11-20',
                          'status': 'open', 'received_at': '2026-09-25T00:00:00Z'}]}
        default_result = collection_forecast(dataset, {'promise_mode': 'default'}, AS_OF)
        anchor_result = collection_forecast(dataset, {'promise_mode': 'anchor'}, AS_OF)
        # 锚定把部分概率质量从近周推到承诺日所在的远周
        self.assertGreater(
            sum(anchor_result['scenarios'][0]['expected_weekly_cents'][4:]),
            sum(default_result['scenarios'][0]['expected_weekly_cents'][4:]))
        self.assertLess(
            sum(anchor_result['scenarios'][0]['expected_weekly_cents'][:4]),
            sum(default_result['scenarios'][0]['expected_weekly_cents'][:4]))
        # 锚定只改变时间，不改变总额
        self.assertEqual(anchor_result['scenarios'][0]['expected_total_cents'],
                         default_result['scenarios'][0]['expected_total_cents'])


class CorrelationAndScenarioTests(unittest.TestCase):
    def setUp(self):
        self.dataset = seed_dataset()

    def test_common_shock_widens_portfolio_bands(self):
        result = forecast(self.dataset, paths=4000)
        baseline = result['scenarios'][0]
        for correlated, independent in zip(baseline['weekly_bands_cents'],
                                           baseline['independent_weekly_bands_cents']):
            correlated_width = correlated['p90_cents'] - correlated['p10_cents']
            independent_width = independent['p90_cents'] - independent['p10_cents']
            self.assertGreaterEqual(correlated_width, independent_width)
        # 联合缺口在同路径上分解，分量之和严格相等；共同冲击下风险不低于独立假设
        self.assertEqual(
            baseline['gap_at_risk_cents']['p10_cents'],
            baseline['gap_tail_cents']['p10_cents']
            + baseline['gap_uncollectible_cents']['p10_cents'])
        self.assertGreaterEqual(
            baseline['gap_at_risk_cents']['p10_cents'],
            baseline['independent_gap_at_risk_cents']['p10_cents'])
        self.assertIn('p50_cents', baseline['gap_at_risk_cents'])

    def test_client_delay_scenario_pushes_cash_later(self):
        result = forecast(self.dataset, scenarios=[
            {'name': 'c3延迟', 'clients': {'c3': {'delay_days': 21}}}])
        baseline, stressed = result['scenarios']
        base_client = next(c for c in baseline['client_forecasts'] if c['client'] == 'c3')
        stressed_client = next(c for c in stressed['client_forecasts'] if c['client'] == 'c3')
        self.assertGreater(
            sum(stressed_client['expected_weekly_cents'][2:]),
            sum(base_client['expected_weekly_cents'][2:]))
        self.assertEqual(stressed['expected_total_cents'], baseline['expected_total_cents'])

    def test_nonpay_scenario_raises_uncollectible(self):
        result = forecast(self.dataset, scenarios=[
            {'name': 'c8违约', 'clients': {'c8': {'add_nonpay': 0.40}}}])
        baseline, stressed = result['scenarios']
        self.assertGreater(stressed['expected_uncollectible_cents'],
                           baseline['expected_uncollectible_cents'])
        self.assertGreater(stressed['gap_at_risk_cents']['p50_cents'], 0)

    def test_unknown_client_in_scenario_is_rejected(self):
        with self.assertRaises(ValueError):
            forecast(self.dataset, scenarios=[
                {'name': '错客户', 'clients': {'ghost': {'delay_days': 7}}}])

    def test_invalid_options_are_rejected(self):
        for bad in ({'weeks': 0}, {'paths': 10}, {'seed': 'x'},
                    {'promise_mode': 'nope'},
                    {'scenarios': [{'clients': {}}]},
                    {'scenarios': [{'name': 'x', 'clients': {'c3': {'delay_days': 999}}}]},
                    {'scenarios': [{'name': 'x', 'clients': {'c3': {'add_nonpay': 2}}}]}):
            with self.assertRaises(ValueError):
                forecast(self.dataset, **bad)


class SyntheticContractTests(unittest.TestCase):
    def _dataset_without_history(self):
        return {
            'invoices': [
                {'invoice_id': 'i1', 'client': 'new', 'gross_cents': 100000,
                 'issued_at': '2026-09-01', 'due_at': '2026-10-15',
                 'received_at': '2026-09-01T00:00:00Z'}],
            'cash_events': [], 'allocations': [], 'promises': []}

    def test_client_without_settlement_history_is_kept_low_confidence(self):
        result = forecast(self._dataset_without_history())
        self.assertEqual(len(result['client_behavior']), 1)
        profile = result['client_behavior'][0]
        self.assertFalse(profile['has_settlement_history'])
        self.assertEqual(profile['confidence'], 'low')
        self.assertEqual(profile['shrink_weight'], 0.0)
        baseline = result['scenarios'][0]
        self.assertEqual(baseline['expected_total_cents'], 100000)
        # 未到期发票不应被压在第一周
        self.assertLess(baseline['expected_weekly_cents'][0], 100000)

    def test_cutoff_only_uses_visible_records(self):
        from app.contracts import visible
        dataset = self._dataset_without_history()
        dataset['cash_events'] = [
            {'payment_id': 'p1', 'client': 'new', 'amount_cents': 50000,
             'paid_at': '2026-10-20', 'received_at': '2026-10-21T00:00:00Z'}]
        dataset['allocations'] = [
            {'allocation_id': 'x1', 'payment_id': 'p1', 'invoice_id': 'i1',
             'amount_cents': 50000, 'revision': 1, 'status': 'active',
             'received_at': '2026-10-21T00:00:00Z'}]
        snapshot = visible(dataset, AS_OF)
        result = collection_forecast(snapshot, {}, AS_OF)
        # 截止后到账不可见，发票仍全额未清
        self.assertEqual(result['reconciliation']['open_cents'], 100000)

    def test_empty_tables_do_not_crash(self):
        result = forecast({table: [] for table in
                           ['invoices', 'cash_events', 'allocations', 'promises']})
        self.assertEqual(result['reconciliation']['open_cents'], 0)
        self.assertEqual(result['scenarios'][0]['expected_total_cents'], 0)

    def test_source_is_not_mutated_and_output_is_deterministic(self):
        dataset = seed_dataset()
        before = copy.deepcopy(dataset)
        first = forecast(dataset, paths=300)
        second = forecast(dataset, paths=300)
        self.assertEqual(dataset, before)
        self.assertEqual(first, second)

    def test_forecast_is_registered_operation(self):
        self.assertIn('collection_forecast', OPERATIONS)
