"""Provider-reported token counts. Missing counters stay unknown; these are not plan quota."""
import json

FIELDS = ('input_total', 'input_uncached', 'cache_read', 'cache_write', 'output')


def input_total(metrics):
    if metrics.get('input_total') is not None:
        return metrics['input_total']
    values = [metrics.get(k) for k in ('input_uncached', 'cache_read', 'cache_write')]
    return sum(values) if all(v is not None for v in values) else None


def normalize(provider, usage):
    def get(value, key, default=None):
        return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)
    if provider in ('anthropic', 'claude-sdk'):
        result = dict(input_uncached=get(usage, 'input_tokens'), cache_read=get(usage, 'cache_read_input_tokens'),
                      cache_write=get(usage, 'cache_creation_input_tokens'), output=get(usage, 'output_tokens'))
        result['input_total'] = input_total(result)
        return result
    total = get(usage, 'input_tokens', get(usage, 'prompt_tokens'))
    details = get(usage, 'input_tokens_details', get(usage, 'prompt_tokens_details'))
    cached, written = get(details, 'cached_tokens'), get(details, 'cache_write_tokens')
    uncached = total - cached - written if all(v is not None for v in (total, cached, written)) else None
    return dict(input_total=total, cache_read=cached, cache_write=written,
                input_uncached=uncached, output=get(usage, 'output_tokens', get(usage, 'completion_tokens')))


def aggregate(records):
    known = dict.fromkeys(FIELDS, 0)
    unknown = dict.fromkeys(FIELDS, 0)
    count = 0
    for metrics in records:
        count += 1
        for field in FIELDS:
            value = input_total(metrics) if field == 'input_total' else metrics.get(field)
            if value is None:
                unknown[field] += 1
                known[field] += (metrics.get("known_subtotals") or {}).get(field) or 0
            else:
                known[field] += value
    result = {k: None if unknown[k] else known[k] for k in FIELDS}
    result.update(known_subtotals=known, unknown_fields=unknown, responses=count)
    return result


def decode(raw, role=''):
    metrics = json.loads(raw or '{}')
    if metrics.get('input_total') is not None and metrics.get('cache_write') is None:
        metrics['input_uncached'] = None  # Older OpenAI rows did not separate cache writes.
    # Old SDK stream/residual rows contain character estimates, not reported output.
    if role in ('sdk_stream', 'sdk_aggregate'):
        metrics['output'] = None
    return metrics
