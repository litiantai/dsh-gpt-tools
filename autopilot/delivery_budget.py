"""交付评审、返修与独立验证共用每日独立 Token 预算。"""

DEFAULT_LIMIT = 10000000


def charged(action):
    """只限制消耗模型的评审、修复及验证，不阻塞已通过成果的合并。"""
    return action.startswith(('review', 'repair', 'validate'))


def snapshot(ledger, product):
    """从持久化用量账本读取当日额度；与开发预算独立。"""
    limit = product.get('code_review', {}).get('tokens_per_day', DEFAULT_LIMIT)
    used = ledger.budget_used(product['id'], 'code_delivery_tokens')
    return {'used': used, 'limit': limit, 'exhausted': bool(limit and used >= limit)}


def wait_reason(ledger, product, action):
    """配置提高额度或跨北京时间午夜后自动继续，不消耗评审轮次。"""
    if charged(action) and snapshot(ledger, product)['exhausted']:
        return '今日交付 Token 额度已用尽，等待北京时间次日零点或调整交付额度'
    return ''
