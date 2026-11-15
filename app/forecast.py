"""回款分布预测。

口径要点：
- 以 ledger.reconcile 重建的截止时点真正未清金额为唯一基数，与 open_balance 完全对齐；
- 历史行为以 payment_id 到账事件为样本单位（一笔到账可分摊多张发票），
  绝不把每条 allocation 核销记录当作独立样本；revision/reversed 已在核销层处理；
- 客户级参数小样本向组合均值收缩；无结清记录的客户保留在输出中并明确低把握；
- 承诺不是现金：只用于违约信号与可选的时间锚，不进入任何实收金额；
- 蒙特卡洛让所有客户共享市场冲击因子，体现同一客户组合的共同延误，
  汇总区间因此不能由各发票独立区间相加得到，另给独立假设对照。
"""

import math
import random
from collections import defaultdict
from datetime import date

from app.contracts import instant, latest
from app.ledger import reconcile

# ---- 显式建模常量（方法说明中同步披露） ----
SHRINK_KAPPA = 3.0            # 小样本向组合均值收缩的等效样本量
RESIDUAL_FLOOR_DAYS = 2.0     # 客户级随机延误波动下限（天）
MARKET_SIGMA_RATIO = 0.5      # 市场共同波动 = 组合延误标准差的比例
MARKET_SIGMA_FLOOR = 1.5      # 市场共同波动下限（天）
BASE_NONPAY = 0.02            # 无坏账记录时的保守先验拖欠率
BROKEN_PROMISE_PENALTY = 0.05  # 每个到期未兑现承诺叠加的拖欠概率
NONPAY_CAP = 0.60
LAPLACE_ALPHA = 1.0           # 承诺兑现率的拉普拉斯平滑
ANCHOR_PRIOR_WEIGHT = 0.5     # 无成熟承诺可参考时，anchor 模式采用的中性先验权重
DEFAULT_WEEKS = 8
DEFAULT_PATHS = 2000
DEFAULT_SEED = 20260930
QUANTILES = (0.10, 0.50, 0.90)


def _day(value):
    return instant(value).date()


def _normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _percentile(sorted_values, q):
    if not sorted_values:
        return 0.0
    position = q * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _validate_options(options):
    if not isinstance(options, dict):
        raise ValueError('预测选项须为对象')
    weeks = options.get('weeks', DEFAULT_WEEKS)
    paths = options.get('paths', DEFAULT_PATHS)
    seed = options.get('seed', DEFAULT_SEED)
    if not isinstance(weeks, int) or not 1 <= weeks <= 26:
        raise ValueError('weeks 须为 1 至 26 的整数')
    if not isinstance(paths, int) or not 100 <= paths <= 20000:
        raise ValueError('paths 须为 100 至 20000 的整数')
    if not isinstance(seed, int):
        raise ValueError('seed 须为整数')
    promise_mode = options.get('promise_mode', 'default')
    if promise_mode not in {'default', 'ignore', 'anchor'}:
        raise ValueError('promise_mode 仅支持 default、ignore、anchor')
    scenarios = options.get('scenarios', [])
    if not isinstance(scenarios, list) or len(scenarios) > 10:
        raise ValueError('scenarios 须为至多 10 个情景的数组')
    cleaned = []
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get('name'), str) \
                or not scenario['name'].strip():
            raise ValueError('每个情景须带非空 name')
        overrides = scenario.get('clients', {})
        if not isinstance(overrides, dict):
            raise ValueError('情景 clients 须为按客户编号的覆盖对象')
        clean_clients = {}
        for client, override in overrides.items():
            if not isinstance(override, dict):
                raise ValueError('客户情景覆盖须为对象')
            delay_days = override.get('delay_days', 0)
            add_nonpay = override.get('add_nonpay', 0.0)
            if not isinstance(delay_days, int) or not -30 <= delay_days <= 180:
                raise ValueError('delay_days 须为 -30 至 180 的整数')
            if not isinstance(add_nonpay, (int, float)) or not 0.0 <= add_nonpay <= 0.9:
                raise ValueError('add_nonpay 须为 0 至 0.9 之间的数')
            clean_clients[client] = {'delay_days': delay_days,
                                     'add_nonpay': float(add_nonpay)}
        cleaned.append({'name': scenario['name'], 'clients': clean_clients})
    return weeks, paths, seed, promise_mode, cleaned


def _payment_samples(facts):
    """以到账事件为样本单位，计算每笔 payment 相对其核销发票加权到期日的延误天数。"""
    invoices = facts['invoices']
    payments = facts['payments']
    grouped = defaultdict(list)
    for allocation in facts['active_allocations']:
        grouped[allocation['payment_id']].append(allocation)
    samples = []
    for payment_id, allocations in grouped.items():
        payment = payments[payment_id]
        weighted_due = 0.0
        total = 0
        for allocation in allocations:
            amount = allocation['amount_cents']
            weighted_due += _day(invoices[allocation['invoice_id']]['due_at']).toordinal() * amount
            total += amount
        if total <= 0:
            continue
        samples.append({
            'client': payment['client'],
            'payment_id': payment_id,
            'delay_days': _day(payment['paid_at']).toordinal() - weighted_due / total,
        })
    return samples


def _client_behavior(facts, samples):
    by_client = defaultdict(list)
    for sample in samples:
        by_client[sample['client']].append(sample['delay_days'])
    all_delays = [sample['delay_days'] for sample in samples]
    if all_delays:
        pooled_mean = sum(all_delays) / len(all_delays)
        pooled_std = math.sqrt(sum((value - pooled_mean) ** 2 for value in all_delays)
                               / len(all_delays))
    else:
        pooled_mean, pooled_std = 0.0, 0.0
    profiles = {}
    for client in sorted({row['client'] for row in facts['invoices'].values()}):
        delays = by_client.get(client, [])
        count = len(delays)
        if count:
            sample_mean = sum(delays) / count
            sample_std = math.sqrt(sum((value - sample_mean) ** 2 for value in delays) / count)
            weight = count / (count + SHRINK_KAPPA)
            mean = weight * sample_mean + (1.0 - weight) * pooled_mean
            sigma = max(sample_std * weight + pooled_std * (1.0 - weight),
                        RESIDUAL_FLOOR_DAYS)
        else:
            sample_mean = sample_std = None
            mean, sigma = pooled_mean, max(pooled_std, RESIDUAL_FLOOR_DAYS)
        confidence = 'high' if count >= 8 else ('medium' if count >= 4 else 'low')
        profiles[client] = {
            'client': client,
            'settlement_events': count,
            'has_settlement_history': count > 0,
            'confidence': confidence,
            'shrink_weight': count / (count + SHRINK_KAPPA),
            'observed_mean_delay_days': sample_mean,
            'observed_std_delay_days': sample_std,
            'fitted_mean_delay_days': mean,
            'fitted_sigma_days': sigma,
        }
    market_sigma = max(pooled_std * MARKET_SIGMA_RATIO, MARKET_SIGMA_FLOOR)
    return profiles, pooled_mean, pooled_std, market_sigma


def _promise_stats(snapshot, facts, as_of_day):
    records = latest(snapshot['promises'], ['promise_id'])
    open_latest = [row for row in records if row['status'] != 'withdrawn']
    withdrawn = len(records) - len(open_latest)
    matured_broken = {}
    future = {}
    for row in open_latest:
        client = row['client']
        expected = _day(row['expected_date'])
        revised_at = _day(row['received_at'])
        if expected < as_of_day:
            # 无承诺-到账关联字段，只承认窗口内净新增到账：
            # 窗口内到账须先覆盖该笔到账已经核销的金额，剩余未分摊部分
            # 才能算作对承诺的兑现；常规、已被核销的回款不算。
            window_payments = [
                payment for payment in facts['payments'].values()
                if payment['client'] == client
                and revised_at <= _day(payment['paid_at']) <= expected
            ]
            window_ids = {payment['payment_id'] for payment in window_payments}
            window_cash = sum(payment['amount_cents'] for payment in window_payments)
            window_allocated = sum(
                allocation['amount_cents']
                for allocation in facts['active_allocations']
                if allocation['payment_id'] in window_ids)
            if window_cash - window_allocated < row['amount_cents']:
                matured_broken[client] = matured_broken.get(client, 0) + 1
        else:
            future[client] = {'amount_cents': row['amount_cents'],
                              'expected_date': row['expected_date']}
    total_matured = sum(1 for row in open_latest if _day(row['expected_date']) < as_of_day)
    reliability = (LAPLACE_ALPHA + total_matured - sum(matured_broken.values())) / \
                  (2 * LAPLACE_ALPHA + total_matured) if total_matured else None
    return {
        'open_promises': len(open_latest),
        'withdrawn_promises': withdrawn,
        'matured_promises': total_matured,
        'matured_broken_promises': sum(matured_broken.values()),
        'kept_promise_rate_smoothed': reliability,
        'matured_broken_by_client': matured_broken,
        'future_promises': future,
        'verifiable': False,
        'note': '数据无承诺与到账的核销关联字段；仅当承诺窗口内存在足以覆盖承诺金额的未分摊到账才视为兑现，'
                '常规已核销回款不算，无法验证的承诺按未兑现处理。承诺永不计入实收。',
    }


def _build_params(snapshot, facts, as_of_day, promise_mode):
    samples = _payment_samples(facts)
    profiles, pooled_mean, pooled_std, market_sigma = _client_behavior(facts, samples)
    promises = _promise_stats(snapshot, facts, as_of_day)
    reliability = promises['kept_promise_rate_smoothed']
    anchor_weight = reliability if reliability is not None else ANCHOR_PRIOR_WEIGHT
    open_by_client = defaultdict(list)
    for row in facts['balances']:
        if row['open_cents'] > 0:
            open_by_client[row['client']].append(row)
    params = {}
    for client, balances in open_by_client.items():
        profile = profiles[client]
        broken = 0 if promise_mode == 'ignore' else promises['matured_broken_by_client'].get(client, 0)
        base_nonpay = min(BASE_NONPAY + broken * BROKEN_PROMISE_PENALTY, NONPAY_CAP)
        invoice_lags = []
        for balance in balances:
            model_lag = max(0.0, (_day(balance['due_at']) - as_of_day).days
                            + profile['fitted_mean_delay_days'])
            expected_lag = model_lag
            if promise_mode == 'anchor' and client in promises['future_promises']:
                promise_lag = max(
                    0.0,
                    (_day(promises['future_promises'][client]['expected_date']) - as_of_day).days)
                expected_lag = (1.0 - anchor_weight) * model_lag + anchor_weight * promise_lag
            invoice_lags.append({'invoice_id': balance['invoice_id'],
                                 'open_cents': balance['open_cents'],
                                 'expected_lag_days': expected_lag})
        params[client] = {
            'profile': profile,
            'base_nonpay': base_nonpay,
            'broken_promises': broken,
            'invoices': invoice_lags,
            'open_cents': sum(item['open_cents'] for item in invoice_lags),
        }
    return params, profiles, pooled_mean, pooled_std, market_sigma, promises, reliability


def _analytic_weeks(params, weeks, nonpay_adjust=None, delay_adjust=None):
    weekly = [0.0] * weeks
    tail = 0.0
    uncollectible = 0.0
    per_client = []
    for client in sorted(params):
        spec = params[client]
        nonpay = spec['base_nonpay']
        if nonpay_adjust and client in nonpay_adjust:
            nonpay = min(NONPAY_CAP, nonpay + nonpay_adjust[client])
        extra_delay = delay_adjust.get(client, 0) if delay_adjust else 0
        client_weekly = [0.0] * weeks
        client_tail = 0.0
        client_uncollectible = 0.0
        for item in spec['invoices']:
            amount = item['open_cents']
            bad = amount * nonpay
            collectible = amount - bad
            mean_lag = item['expected_lag_days'] + extra_delay
            sigma = max(spec['profile']['fitted_sigma_days'], RESIDUAL_FLOOR_DAYS)
            bounds = [_normal_cdf((week * 7 - mean_lag) / sigma) for week in range(weeks + 1)]
            for week in range(weeks):
                client_weekly[week] += collectible * (bounds[week + 1] - bounds[week])
            client_tail += collectible * (1.0 - bounds[weeks])
            client_uncollectible += bad
        rounded_weekly = [round(value) for value in client_weekly]
        rounded_tail = round(client_tail)
        rounded_uncollectible = round(client_uncollectible)
        # 取整残差在客户内部并入第一周，保证客户层与组合层都逐分守恒
        rounded_weekly[0] += spec['open_cents'] - (
            sum(rounded_weekly) + rounded_tail + rounded_uncollectible)
        weekly = [weekly[i] + rounded_weekly[i] for i in range(weeks)]
        tail += rounded_tail
        uncollectible += rounded_uncollectible
        per_client.append({'client': client,
                           'open_cents': spec['open_cents'],
                           'expected_weekly_cents': rounded_weekly,
                           'expected_tail_cents': rounded_tail,
                           'expected_uncollectible_cents': rounded_uncollectible,
                           'nonpay_probability': nonpay})
    return weekly, tail, uncollectible, per_client


def _simulate(params, weeks, horizon_days, paths, seed, market_sigma,
              correlated, nonpay_adjust=None, delay_adjust=None):
    rng = random.Random(seed)
    clients = sorted(params)
    weekly_paths = [[0] * weeks for _ in range(paths)]
    tail_paths = [0] * paths
    uncollectible_paths = [0] * paths
    for path in range(paths):
        market = rng.gauss(0.0, market_sigma) if correlated else 0.0
        for client in clients:
            spec = params[client]
            nonpay = spec['base_nonpay']
            if nonpay_adjust and client in nonpay_adjust:
                nonpay = min(NONPAY_CAP, nonpay + nonpay_adjust[client])
            if rng.random() < nonpay:
                uncollectible_paths[path] += spec['open_cents']
                continue
            extra_delay = delay_adjust.get(client, 0) if delay_adjust else 0
            client_sigma = max(spec['profile']['fitted_sigma_days'], RESIDUAL_FLOOR_DAYS)
            mean_lag = sum(item['expected_lag_days'] * item['open_cents']
                           for item in spec['invoices']) / spec['open_cents']
            lag = max(0.0, mean_lag + extra_delay + market + rng.gauss(0.0, client_sigma))
            if lag >= horizon_days:
                tail_paths[path] += spec['open_cents']
            else:
                weekly_paths[path][min(int(lag // 7), weeks - 1)] += spec['open_cents']
    return weekly_paths, tail_paths, uncollectible_paths


def _summarize(weekly_paths, tail_paths, uncollectible_paths, weeks):
    columns = []
    running = [0] * len(weekly_paths)
    cumulative_p10 = []
    for week in range(weeks):
        values = sorted(path_row[week] for path_row in weekly_paths)
        for index, path_row in enumerate(weekly_paths):
            running[index] += path_row[week]
        columns.append({'week': week + 1,
                        'p10_cents': round(_percentile(values, 0.10)),
                        'p50_cents': round(_percentile(values, 0.50)),
                        'p90_cents': round(_percentile(values, 0.90))})
        cumulative_p10.append(round(_percentile(sorted(running), 0.10)))
    # 缺口在同一条路径上分解为「期外迟付」与「不可收」，分量之和严格等于总缺口
    gap_rows = sorted(
        (tail_paths[index] + uncollectible_paths[index],
         tail_paths[index], uncollectible_paths[index])
        for index in range(len(tail_paths)))
    gap_p10, gap_tail_p10, gap_uncollectible_p10 = gap_rows[
        math.ceil(0.10 * (len(gap_rows) - 1))]
    gap_p50, gap_tail_p50, gap_uncollectible_p50 = gap_rows[
        int(0.50 * (len(gap_rows) - 1))]
    tail_values = sorted(tail_paths)
    uncollectible_values = sorted(uncollectible_paths)
    return columns, cumulative_p10, {
        'p10_cents': round(_percentile(tail_values, 0.10)),
        'p50_cents': round(_percentile(tail_values, 0.50)),
        'p90_cents': round(_percentile(tail_values, 0.90))}, {
        'p10_cents': round(_percentile(uncollectible_values, 0.10)),
        'p50_cents': round(_percentile(uncollectible_values, 0.50)),
        'p90_cents': round(_percentile(uncollectible_values, 0.90))}, \
        {'p10_cents': round(gap_p10), 'p50_cents': round(gap_p50)}, \
        {'p10_cents': round(gap_tail_p10), 'p50_cents': round(gap_tail_p50)}, \
        {'p10_cents': round(gap_uncollectible_p10),
         'p50_cents': round(gap_uncollectible_p50)}


def _scenario_payload(name, params, weeks, horizon_days, paths, seed, market_sigma,
                      nonpay_adjust, delay_adjust):
    weekly, tail, uncollectible, per_client = _analytic_weeks(
        params, weeks, nonpay_adjust, delay_adjust)
    # 取整残差已在每个客户内部消化，组合层为整数加总，天然与未清账本逐分对齐
    expected_weekly = [int(value) for value in weekly]
    correlated = _simulate(params, weeks, horizon_days, paths, seed, market_sigma,
                           True, nonpay_adjust, delay_adjust)
    independent = _simulate(params, weeks, horizon_days, paths, seed, market_sigma,
                            False, nonpay_adjust, delay_adjust)
    columns, cumulative_p10, tail_band, uncollectible_band, gap_band, gap_tail_band, \
        gap_uncollectible_band = _summarize(*correlated, weeks)
    ind_columns, ind_cumulative_p10, _, _, independent_gap_band, _, _ = \
        _summarize(*independent, weeks)
    return {
        'name': name,
        'expected_weekly_cents': expected_weekly,
        'expected_tail_cents': tail,
        'expected_uncollectible_cents': uncollectible,
        'expected_total_cents': sum(expected_weekly) + tail + uncollectible,
        'weekly_bands_cents': columns,
        'cumulative_p10_cents': cumulative_p10,
        'tail_band_cents': tail_band,
        'uncollectible_band_cents': uncollectible_band,
        'independent_weekly_bands_cents': ind_columns,
        'independent_cumulative_p10_cents': ind_cumulative_p10,
        # 联合缺口在同路径上分解为「期外迟付」与「不可收」，分量之和严格等于总量
        'gap_at_risk_cents': gap_band,
        'gap_tail_cents': gap_tail_band,
        'gap_uncollectible_cents': gap_uncollectible_band,
        'independent_gap_at_risk_cents': independent_gap_band,
        'client_forecasts': per_client,
    }


def collection_forecast(snapshot, options, as_of):
    weeks, paths, seed, promise_mode, scenarios = _validate_options(options)
    if as_of is None:
        raise ValueError('预测需要明确截止时间 as_of')
    as_of_day = instant(as_of).date()
    horizon_days = weeks * 7
    facts = reconcile(snapshot)
    params, profiles, pooled_mean, pooled_std, market_sigma, promises, reliability = \
        _build_params(snapshot, facts, as_of_day, promise_mode)

    open_total = sum(row['open_cents'] for row in facts['balances'])
    gross_total = sum(row['gross_cents'] for row in facts['balances'])
    allocated_total = sum(facts['allocated_by_invoice'].values())

    baseline = _scenario_payload('baseline', params, weeks, horizon_days, paths, seed,
                                 market_sigma, None, None)
    scenario_results = [baseline]
    for index, scenario in enumerate(scenarios, start=1):
        unknown = sorted(set(scenario['clients']) - set(params))
        if unknown:
            raise ValueError('情景引用了没有未清余额的客户：' + ','.join(unknown))
        nonpay_adjust = {client: override['add_nonpay']
                         for client, override in scenario['clients'].items()
                         if override['add_nonpay'] > 0}
        delay_adjust = {client: override['delay_days']
                        for client, override in scenario['clients'].items()
                        if override['delay_days'] != 0}
        scenario_results.append(_scenario_payload(
            scenario['name'], params, weeks, horizon_days, paths,
            seed + index * 7919, market_sigma, nonpay_adjust, delay_adjust))

    method = {
        'sample_unit': 'payment_id 到账事件（多发票分摊合并为一笔），核销修订与 reversed 已在对账层剔除',
        'delay_definition': '到账日相对该笔到账所核销发票按金额加权到期日的天数，正值为迟付',
        'shrinkage': f'客户延误均值按 n/(n+{int(SHRINK_KAPPA)}) 向组合均值收缩，无结清记录客户直接采用组合先验',
        'weekly_model': '每张未清发票的期望到账服从以预期滞后天数为均值、客户波动为标准差的正态分布，按周切分概率质量',
        'nonpayment': f'数据无坏账核销记录，基础拖欠率为先验 {BASE_NONPAY:.0%}；'
                      f'每个到期未兑现承诺叠加 {BROKEN_PROMISE_PENALTY:.0%}，封顶 {NONPAY_CAP:.0%}',
        'promise_reliability_smoothed': reliability,
        'promise_policy': '承诺一律不计入实收；promise_mode=anchor 时用平滑兑现率把时间均值向承诺日部分靠拢，'
                          f'无成熟承诺可参考时采用中性先验权重 {ANCHOR_PRIOR_WEIGHT}',
        'correlation': f'蒙特卡洛中所有客户共享 N(0, {market_sigma:.2f}天) 市场冲击，'
                       '体现共同延误；independent_* 为去掉共同因子的独立假设对照，区间系统性偏窄',
        'simulation': {'paths': paths, 'seed': seed, 'quantiles': list(QUANTILES),
                       'market_sigma_days': market_sigma,
                       'pooled_mean_delay_days': pooled_mean,
                       'pooled_std_delay_days': pooled_std},
        'constants': {'shrink_kappa': SHRINK_KAPPA,
                      'residual_floor_days': RESIDUAL_FLOOR_DAYS,
                      'market_sigma_ratio': MARKET_SIGMA_RATIO,
                      'market_sigma_floor_days': MARKET_SIGMA_FLOOR,
                      'base_nonpay': BASE_NONPAY,
                      'broken_promise_penalty': BROKEN_PROMISE_PENALTY,
                      'nonpay_cap': NONPAY_CAP, 'laplace_alpha': LAPLACE_ALPHA,
                      'anchor_prior_weight': ANCHOR_PRIOR_WEIGHT},
    }
    return {
        'as_of_date': as_of_day.isoformat(),
        'horizon': {
            'weeks': weeks,
            'start_date': date.fromordinal(as_of_day.toordinal() + 1).isoformat(),
            'end_date': date.fromordinal(as_of_day.toordinal() + horizon_days).isoformat()},
        'reconciliation': {
            'gross_cents': gross_total,
            'allocated_cents': allocated_total,
            'open_cents': open_total,
            'identity_check': gross_total == open_total + allocated_total,
            'forecast_distributes_all_open': baseline['expected_total_cents'] == open_total,
        },
        'client_behavior': list(profiles.values()),
        'promises': promises,
        'scenarios': scenario_results,
        'method': method,
    }
