"""从模型原始用量回执累计 Token；缓存 Token 计入总量，重复读取不会重复扣减。"""
import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def trace_tokens(path):
    total=0
    seen=set()
    for line in Path(path).read_text(errors='replace').splitlines():
        try:
            event=json.loads(line)
        except ValueError:
            continue
        usage=event.get('usage') or {}
        if event.get('type')=='turn.completed':
            # Codex input_tokens already includes cached input; reasoning is part of output.
            total+=usage.get('input_tokens',0)+usage.get('output_tokens',0)
        elif event.get('type')=='result' and usage:
            # Claude CLI reports cache counts separately from input tokens.
            total+=sum(usage.get(k,0) for k in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens'))
        elif event.get('type')=='status' and event.get('phase')=='step_end' and usage:
            key=(event.get('turn'),event.get('step'))
            if key in seen:
                continue
            seen.add(key)
            total+=usage.get('totalTokens',sum(usage.get(k,0) for k in
                         ('inputTokens','outputTokens','cacheReadTokens','cacheWriteTokens')))
    return total


def register(ledger,product_id,path,budget_kind='tokens',record_id=None,action=None):
    if budget_kind == 'tokens' and action in ('discover', 'investigate'):
        budget_kind = 'discovery_tokens'
    if budget_kind not in ('tokens','daily_report_tokens','code_delivery_tokens','discovery_tokens','chat_tokens','coordination_tokens'):
        raise ValueError('用量分类无效')
    path=str(Path(path).resolve())
    with ledger.store.connect() as db:
        db.execute('INSERT OR IGNORE INTO auto_token_sources(id,product_id,path,budget_kind,record_id,action) VALUES (?,?,?,?,?,?)',
                   (path,product_id,path,budget_kind,record_id,action))


def collect(ledger):
    """持久化增量入账；调度器重启或重复轮询沿用同一用量来源。"""
    with ledger.store.connect() as db:
        rows=db.execute('SELECT * FROM auto_token_sources').fetchall()
    for row in rows:
        path=Path(row['path'])
        if not path.exists():
            continue
        size=path.stat().st_size
        if size==row['size']:
            continue
        tokens=trace_tokens(path)
        with ledger.store.transaction() as db:
            old=db.execute('SELECT tokens FROM auto_token_sources WHERE id=?',(row['id'],)).fetchone()[0]
            delta=max(0,tokens-old)
            day=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            db.execute('INSERT INTO auto_budget VALUES (?,?,?,?) ON CONFLICT(product_id,day,kind) DO UPDATE SET used=used+excluded.used',
                       (row['product_id'],day,row['budget_kind'],delta))
            db.execute('UPDATE auto_token_sources SET tokens=?,size=? WHERE id=?',(max(old,tokens),size,row['id']))
