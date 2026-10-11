"""带版本控制、去重、预算与租约的持久化研发台账。"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo

from review_core import Conflict

KINDS = ('products', 'signals', 'requirements', 'runs', 'releases', 'workers', 'inspections', 'evidence', 'evaluations', 'daily_reports', 'deliveries', 'code_reviews', 'scans', 'conversations', 'chat_messages', 'attachments', 'competitors', 'research_jobs', 'source_snapshots', 'agent_messages', 'notifications', 'problems', 'test_chains', 'test_chain_versions', 'test_chain_runs', 'test_chain_baselines', 'coordinators', 'coordination_decisions')
TERMINAL = {'completed', 'accepted', 'cancelled', 'rolled_back', 'delivered', 'online'}
DEFAULTS = dict(probe_seconds=60,inspection_seconds=21600, discovery_per_day=4, runs_per_day=2,
                tokens_per_day=0, deepseek_off_peak_only=True,
                execution_seconds=3600, max_revisions=3, idle_seconds=300,
                observation_seconds=1800)
SECRET = re.compile(r'(?i)(bearer\s+)[\w.\-/+=]+|((?:token|password|cookie|authorization|api[_-]?key)\s*[=:]\s*)[^\s,;]+')


def redact(value):
    """递归清除凭据字段及常见日志凭据；原始账户资料不得作为输入。"""
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'(?i)token|password|cookie|authorization|api.?key', k)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return SECRET.sub(lambda m: (m[1] or m[2]) + '[redacted]', value)[:20000]
    return value


class Ledger:
    """所有状态转换在 SQLite 事务中提交，操作日志与状态保持一致。"""
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            for kind in KINDS:
                db.execute(f'''CREATE TABLE IF NOT EXISTS auto_{kind} (
                    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    data TEXT NOT NULL)''')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS auto_dedup (product_id TEXT, fingerprint TEXT,
                    signal_id TEXT NOT NULL, PRIMARY KEY(product_id,fingerprint));
                CREATE TABLE IF NOT EXISTS auto_budget (product_id TEXT, day TEXT, kind TEXT,
                    used INTEGER NOT NULL, PRIMARY KEY(product_id,day,kind));
                CREATE TABLE IF NOT EXISTS auto_leases (name TEXT PRIMARY KEY, owner TEXT,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_token_sources (id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
                    path TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT -1);
                CREATE INDEX IF NOT EXISTS auto_runs_status ON auto_runs(status,created);
            ''')

            for kind in KINDS:
                db.execute(f'CREATE INDEX IF NOT EXISTS auto_{kind}_project ON auto_{kind}(product_id,created,id)')
            if 'budget_kind' not in {r[1] for r in db.execute('PRAGMA table_info(auto_token_sources)')}:
                db.execute("ALTER TABLE auto_token_sources ADD COLUMN budget_kind TEXT NOT NULL DEFAULT 'tokens'")
            for column in ('record_id', 'action'):
                if column not in {r[1] for r in db.execute('PRAGMA table_info(auto_token_sources)')}:
                    db.execute(f"ALTER TABLE auto_token_sources ADD COLUMN {column} TEXT")

    def _table(self, kind):
        if kind not in KINDS:
            raise KeyError('台账不存在')
        return 'auto_' + kind

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError('记录不存在')
        return json.loads(row['data']) | {k: row[k] for k in ('id','product_id','status','version','created','updated')}

    def list(self, kind):
        with self.store.connect() as db:
            return [self.decode(r) for r in db.execute(f'SELECT * FROM {self._table(kind)} ORDER BY created DESC LIMIT 1000')]

    def scoped(self, kind, product_id, db=None, **filters):
        """项目内完整查询；后台不受旧全局列表 1000 条上限影响。"""
        if db is None:
            with self.store.connect() as conn:
                return self.scoped(kind, product_id, conn, **filters)
        rows = [self.decode(row) for row in db.execute(
            f'SELECT * FROM {self._table(kind)} WHERE product_id=? ORDER BY created,id', (product_id,))]
        return [row for row in rows if all(row.get(k) == v for k, v in filters.items())]

    def page(self, kind, product_id, cursor='', limit=50, **filters):
        limit = max(1, min(int(limit), 100))
        clauses, parameters = ['product_id=?'], [product_id]
        for key,value in filters.items():
            if key not in ('conversation_id','run_id','status'):
                raise ValueError('分页筛选字段无效')
            clauses.append('status=?' if key=='status' else "json_extract(data, '$."+key+"')=?")
            parameters.append(value)
        with self.store.connect() as db:
            if cursor:
                anchor = db.execute(f"SELECT created,id FROM {self._table(kind)} WHERE "+' AND '.join(clauses)+' AND id=?', (*parameters,cursor)).fetchone()
                if anchor is None:
                    raise ValueError('分页游标无效')
                clauses.append('(created,id) > (?,?)')
                parameters += [anchor['created'],anchor['id']]
            rows = [self.decode(row) for row in db.execute(f"SELECT * FROM {self._table(kind)} WHERE "+' AND '.join(clauses)+' ORDER BY created,id LIMIT ?', (*parameters,limit+1))]
        return {'items':rows[:limit], 'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    def get(self, kind, ident, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self.get(kind, ident, conn)
        return self.decode(db.execute(f'SELECT * FROM {self._table(kind)} WHERE id=?', (ident,)).fetchone())

    def create(self, kind, data, status='pending', ident=None, db=None):
        if db is None:
            with self.store.transaction() as conn:
                return self.create(kind, data, status, ident, conn)
        ident, now = ident or str(uuid.uuid4()), time.time()
        db.execute(f'INSERT INTO {self._table(kind)} VALUES (?,?,?,?,?,?,?)',
                   (ident, data.get('product_id', ident if kind == 'products' else ''), status, 1, now, now, json.dumps(data, ensure_ascii=False)))
        self.store.event('autopilot_created', detail={'kind':kind,'id':ident,'status':status}, db=db)
        return self.get(kind, ident, db)

    def update(self, kind, ident, version, changes, status=None, db=None):
        if db is None:
            with self.store.transaction() as conn:
                return self.update(kind, ident, version, changes, status, conn)
        old = self.get(kind, ident, db)
        if old['version'] != version:
            raise Conflict('记录已更新，请刷新后重试')
        data = {k:v for k,v in (old | changes).items() if k not in ('id','status','version','created','updated')}
        db.execute(f'UPDATE {self._table(kind)} SET data=?,product_id=?,status=?,version=version+1,updated=? WHERE id=?',
                   (json.dumps(data, ensure_ascii=False), data.get('product_id', old['product_id']), status or old['status'], time.time(), ident))
        if status and status != old['status']:
            self.store.event('autopilot_transition', detail={'kind':kind,'id':ident,'from':old['status'],'to':status}, db=db)
        return self.get(kind, ident, db)

    def signal(self, product_id, body):
        self.get('products', product_id)
        body = redact(body)
        for key in ('source','code','summary','evidence'):
            if not body.get(key):
                raise ValueError(f'信号缺少 {key}')
        fingerprint = hashlib.sha256(json.dumps([body['source'],body['code'],body.get('component',''),body.get('version','')], ensure_ascii=False).encode()).hexdigest()
        with self.store.transaction() as db:
            prior = db.execute('SELECT signal_id FROM auto_dedup WHERE product_id=? AND fingerprint=?', (product_id,fingerprint)).fetchone()
            if prior:
                old = self.get('signals', prior[0], db)
                return self.update('signals', old['id'], old['version'], {'count':old['count']+1,'last_seen':time.time(),'evidence':body['evidence']}, db=db)
            signal = self.create('signals', body | {'product_id':product_id,'fingerprint':fingerprint,'count':1,'last_seen':time.time()}, db=db)
            db.execute('INSERT INTO auto_dedup VALUES (?,?,?)', (product_id,fingerprint,signal['id']))
            return signal

    def budget(self, product_id, kind, limit, db=None):
        if db is None:
            with self.store.transaction() as conn:
                return self.budget(product_id,kind,limit,conn)
        day = datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        args = (product_id,day,kind)
        db.execute('INSERT OR IGNORE INTO auto_budget VALUES (?,?,?,0)', args)
        used = db.execute('SELECT used FROM auto_budget WHERE product_id=? AND day=? AND kind=?',args).fetchone()[0]
        if limit and used >= limit:
            return False
        db.execute('UPDATE auto_budget SET used=used+1 WHERE product_id=? AND day=? AND kind=?',args)
        return True

    def budget_used(self,product_id,kind):
        day=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.store.connect() as db:
            row=db.execute('SELECT used FROM auto_budget WHERE product_id=? AND day=? AND kind=?',
                           (product_id,day,kind)).fetchone()
        return row[0] if row else 0

    def lease(self, name, owner, seconds=30):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM auto_leases WHERE name=?',(name,)).fetchone()
            if row and row['owner'] != owner and row['expires'] > time.time():
                return False
            db.execute('INSERT OR REPLACE INTO auto_leases VALUES (?,?,?)',(name,owner,time.time()+seconds))
            return True

    def metrics(self):
        runs, releases = self.list('runs'), self.list('releases')
        with self.store.connect() as db:
            budgets = [dict(r) for r in db.execute('SELECT * FROM auto_budget ORDER BY day DESC LIMIT 100')]
            leases = [dict(r) for r in db.execute('SELECT * FROM auto_leases')]
        return dict(pending_signals=sum(s['status']=='pending' for s in self.list('signals')),
                    runs=len(runs), completed=sum(r['status']=='completed' for r in runs),
                    revisions=sum(r.get('revisions',0) for r in runs),
                    execution_seconds=sum(r.get('execution_seconds',0) for r in runs),
                    releases=len(releases), successful_releases=sum(r['status']=='completed' for r in releases),
                    rollbacks=sum(r['status']=='rolled_back' for r in releases),budgets=budgets,leases=leases)
