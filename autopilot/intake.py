"""有来源的需求草稿与确认门槛；旧需求保持原有流转方式。"""
from contextlib import nullcontext
import hashlib
import json
import time

from review_core import Conflict
from .store import redact

FIELDS = ('title', 'goal', 'scenario', 'scope', 'acceptance', 'evidence', 'impact', 'questions', 'resolution_probes')


def approved(requirement):
    return not requirement.get('confirmation_required') or requirement.get('confirmation_status') == 'confirmed'


def draft(ledger, product_id, value, source, source_id, db, parent_id=None):
    if not isinstance(value, dict) or not str(value.get('title', '')).strip():
        raise ValueError('需求标题不能为空')
    clean = redact({key: value.get(key, [] if key in ('acceptance', 'questions', 'resolution_probes') else '') for key in FIELDS})
    if not isinstance(clean['acceptance'], list) or not all(isinstance(x, str) for x in clean['acceptance']):
        raise ValueError('验收条件必须为文本列表')
    fingerprint = hashlib.sha256(json.dumps([source, source_id, clean['title'].strip().casefold(), parent_id], ensure_ascii=False).encode()).hexdigest()
    ident = 'draft-' + fingerprint
    existing = db.execute('SELECT * FROM auto_requirements WHERE id=?', (ident,)).fetchone()
    if existing:
        return ledger.decode(existing)
    return ledger.create('requirements', clean | {'product_id': product_id, 'source': source, 'source_id': source_id,
        'parent_requirement_id': parent_id, 'confirmation_required': True, 'confirmation_status': 'pending',
        'in_scope': True, 'classification': 'investigation', 'priority': 2}, 'pending_confirmation', ident=ident, db=db)


def mutate(control, ident, action, body, db=None):
    ledger = control.ledger
    with (nullcontext(db) if db is not None else ledger.store.transaction()) as db:
        item = ledger.get('requirements', ident, db)
        if action == 'confirm' and item.get('confirmation_status') == 'confirmed':
            return item
        if body.get('version') != item['version']:
            raise Conflict('需求已更新，请刷新后核对草稿')
        if action == 'amend':
            return draft(ledger, item['product_id'], item | body.get('draft', {}), 'amendment',
                         body.get('operation_id') or str(item['version']), db, parent_id=ident)
        if not item.get('confirmation_required') or item['status'] != 'pending_confirmation':
            raise Conflict('仅待确认需求可编辑、确认或拒绝；范围变更请建立变更草稿')
        if action == 'edit':
            changes = {k: v for k, v in body.get('draft', {}).items() if k in FIELDS}
            if 'title' in changes and not str(changes['title']).strip():
                raise ValueError('需求标题不能为空')
            for key in ('acceptance', 'questions'):
                if key in changes and (not isinstance(changes[key], list) or not all(isinstance(x, str) for x in changes[key])):
                    raise ValueError('验收条件和待澄清事项必须为文本列表')
            return ledger.update('requirements', ident, item['version'], redact(changes), db=db)
        if action == 'reject':
            return ledger.update('requirements', ident, item['version'], {'confirmation_status': 'rejected'}, 'rejected', db)
        if action != 'confirm':
            raise KeyError('未知需求操作')
        if not all(item.get(k) for k in ('title', 'goal', 'scope', 'acceptance', 'evidence', 'impact')) or item.get('questions'):
            raise ValueError('请补齐目标、范围、证据、影响和验收条件，并解决待澄清事项')
        product = ledger.get('products', item['product_id'], db)
        classification = 'investigation'
        try:
            from .probes import validate
            from .project import generic
            if item.get('resolution_probes'):
                validate(item['resolution_probes'], product if generic(product) else None, assertions=generic(product))
                classification = 'development'
        except ValueError:
            pass
        item = ledger.update('requirements', ident, item['version'], {
            'confirmation_status': 'confirmed', 'confirmed_version': item['version'], 'confirmed_at': time.time(),
            'confirmed_snapshot': {k: item.get(k) for k in FIELDS}, 'classification': classification,
            'reproduction': item.get('scenario') or item['goal'],
        }, 'pending', db)
        if classification == 'development':
            control.queue(item, db=db)
            item = ledger.get('requirements', ident, db)
        return item
