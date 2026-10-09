"""北京时间当日开发 Token 上限；临时调整不覆盖项目默认策略。"""
import datetime as dt
from zoneinfo import ZoneInfo

from review_core import Conflict
from .store import DEFAULTS

ZONE = ZoneInfo('Asia/Shanghai')


def snapshot(ledger, product, now=None):
    now = now or dt.datetime.now(ZONE)
    day = now.astimezone(ZONE).date().isoformat()
    override = product.get('today_token_limit') or {}
    default = (DEFAULTS | product.get('policy', {}))['tokens_per_day']
    overridden = override.get('day') == day
    limit = override['limit'] if overridden else default
    with ledger.store.connect() as db:
        row = db.execute('SELECT used FROM auto_budget WHERE product_id=? AND day=? AND kind=?',
                         (product['id'], day, 'tokens')).fetchone()
    used = row[0] if row else 0
    reset = now.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    return {'day': day, 'tokens_used': used, 'tokens_limit': limit, 'tokens_default_limit': default,
            'tokens_override': overridden, 'tokens_limit_revision': override.get('revision', 0),
            'tokens_reset_at': reset.timestamp(), 'tokens_exhausted': bool(limit and used >= limit)}


def adjust_today(ledger, product_id, body):
    limit = body.get('tokens_limit')
    if type(limit) is not int or not 0 <= limit <= 9007199254740991:
        raise ValueError('今日 Token 上限必须为非负安全整数，0 表示不限')
    with ledger.store.transaction() as db:
        day = dt.datetime.now(ZONE).date().isoformat()
        if body.get('day') != day:
            raise Conflict('北京时间日期已变化，请刷新今日额度后重新调整')
        product = ledger.get('products', product_id, db)
        revision = (product.get('today_token_limit') or {}).get('revision', 0)
        if type(body.get('revision')) is not int or body['revision'] != revision:
            raise Conflict('今日上限已被其他操作修改，请刷新后重新调整')
        ledger.update('products', product_id, product['version'], {'today_token_limit': {
            'day': day, 'limit': limit, 'revision': revision + 1}}, db=db)
        ledger.store.event('autopilot_token_limit_changed', detail={
            'product_id': product_id, 'day': day, 'tokens_limit': limit}, db=db)
    return snapshot(ledger, ledger.get('products', product_id))
