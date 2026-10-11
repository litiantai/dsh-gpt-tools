"""截图驱动的浏览器动作与持续观测；模型只作判断，控制器掌握执行权限。"""
from .role_skills import rule as skill_rule
from abc import ABC, abstractmethod
import json
import os
from pathlib import Path
import select
import shutil
import signal
import threading
import subprocess
import time
import uuid

from .store import Ledger
from .test_chains import settings, TERMINAL
from .evidence import Recorder

ROOT = Path(__file__).resolve().parents[1]


class ComputerUseDriver(ABC):
    """可替换的计算机操作接口；第一版仅开放隔离网页。"""
    @abstractmethod
    def open(self, origin, config): pass
    @abstractmethod
    def screenshot(self, path): pass
    @abstractmethod
    def act(self, action): pass
    @abstractmethod
    def close(self): pass
    @classmethod
    def capabilities(cls):
        return {'driver':'playwright', 'desktop':False, 'browser':True, 'available':bool(shutil.which('node'))}


class BrowserDriver(ComputerUseDriver):
    def __init__(self, root):
        self.root = Path(root)
        self.proc = None
        self.log = None

    def send(self, value):
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError('浏览器执行器已退出')
        self.proc.stdin.write(json.dumps(value) + '\n'); self.proc.stdin.flush()
        if not select.select([self.proc.stdout], [], [], 40)[0]:
            raise RuntimeError('浏览器动作超时')
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError('浏览器会话丢失')
        reply = json.loads(line)
        if not reply.get('ok'):
            raise RuntimeError(reply.get('error', '浏览器动作失败'))
        return reply['result']

    def open(self, origin, config):
        self.log = (self.root/'browser.log').open('a')
        self.proc = subprocess.Popen(['node', str(ROOT/'scripts/computer-use-browser.mjs')], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        return self.send({'command':'open', 'origin':origin, 'allowed_origins':config.get('allowed_origins', []),
                          'storage_state':config.get('storage_state')})

    def screenshot(self, path):
        return self.send({'command':'screenshot', 'path':str(path)})

    def act(self, action):
        return self.send({'command':'action', 'action':action})

    def close(self):
        if self.proc:
            try:
                self.send({'command':'close'})
            except (RuntimeError, OSError, ValueError):
                pass
            for stream in (self.proc.stdin, self.proc.stdout):
                stream.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait()
        if self.log:
            self.log.close()


def model_role(product):
    role = settings(product).get('model') or product.get('agents', {}).get('verification', {})
    if role.get('provider') != 'codex' or not role.get('model'):
        raise ValueError('请在测试链路配置可读取图片的 Codex 模型')
    return role




def decide(request, material, images):
    from .intelligence_worker import run_model
    from .role_skills import output_schema
    properties = output_schema('computer_step')['properties']
    result = run_model('computer_step', request, model_role(request['product']), material,
        skill_rule('computer_use-verification-1'),
        properties, images=images)
    if result.get('status') != 'pass':
        raise RuntimeError(result.get('reason', '模型未完成界面判断'))
    return result


def execute_run(request, run, instance, *, driver_factory=BrowserDriver, judge=decide, sleep=time.sleep, clock=time.monotonic):
    """固定版本执行；每次判断绑定当前截图，观测阶段禁止重放动作。"""
    from review_core import Store
    ledger = Ledger(Store(Path(request['state_root']).parent))
    root = Path(request['state_root'])/'test-chains'/run['id']; root.mkdir(parents=True, exist_ok=True)
    driver = driver_factory(root)
    recorder = Recorder(ledger.store, run['product_id'], run['title'], 'Codex 截图判断 + Playwright 界面操作', 'branch')
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise InterruptedError('执行进程中断，需核对副作用')
        signal.signal(signal.SIGTERM, interrupted)
    config = run['config']
    request = request | {'computer_run_id':run['id'], 'product':request['product'] | {'computer_use':config}}
    start = clock(); request['computer_deadline'] = time.monotonic() + config['timeout_seconds']; actions = 0; tokens = 0; history = []; status = 'blocked'; reason = ''; evidence_ids = []
    def update(changes, state=None):
        with ledger.store.transaction() as db:
            current = ledger.get('test_chain_runs', run['id'], db)
            return ledger.update('test_chain_runs', run['id'], current['version'], changes, state, db)
    def check():
        current = ledger.get('test_chain_runs', run['id'])
        if current.get('cancel_requested'):
            raise InterruptedError('用户已停止运行')
        if instance.get('pid'):
            try:
                os.kill(instance['pid'], 0)
            except (ProcessLookupError, PermissionError) as exc:
                raise RuntimeError('分支测试实例已失联，需核对实例状态') from exc
        from .usage import collect
        from .quota import snapshot as quota_snapshot
        collect(ledger)
        if request.get('budget_kind', 'tokens') == 'tokens' and quota_snapshot(ledger, ledger.get('products',run['product_id']))['tokens_exhausted']:
            raise RuntimeError('每日 Token 额度已用尽')
        if clock() - start >= config['timeout_seconds']:
            raise TimeoutError('运行达到最长等待时间')
    def capture():
        path = root/(str(uuid.uuid4()) + '.png')
        screen = driver.screenshot(path)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError('未获得真实截图')
        return path, screen
    def save(snapshot, step, action, actual, result, path, details):
        evidence = recorder.step(step['goal'], action, step['expected'], actual, status=result, screenshot=path,
            details={'chain_id':snapshot['chain_id'], 'version_id':snapshot['version_id'], 'step_id':step['id'],
                     'run_id':run['id'], 'instance':instance, **details})
        evidence_ids.append(evidence['id'])
        update({'evidence_ids':evidence_ids[:], 'current_step':step['goal'], 'actions':actions,
                'elapsed':clock()-start})
        return evidence
    try:
        if (instance.get('commit') != run['target'].get('commit') or
                instance.get('branch') != run['target'].get('branch') or
                (instance.get('product_id') and instance['product_id'] != run['product_id']) or
                instance.get('stopped') or not instance.get('origin')):
            raise ValueError('测试实例与目标项目、分支、提交不一致或已停止')
        check()
        update({'instance':instance, 'started_at':time.time(), 'driver':'playwright', 'pid':os.getpid()}, 'running')
        driver.open(instance['origin'], config)
        for index, snapshot in enumerate(run['snapshots']):
            if index:
                driver.close()
                driver = driver_factory(root)
                driver.open(instance['origin'], config)
                history = []
            baseline = ledger.get('test_chain_baselines', snapshot['baseline_id']) if snapshot.get('baseline_id') else None
            baseline_evidence = [ledger.get('evidence', ident) for ident in baseline['evidence_ids']] if baseline else []
            for step in snapshot['definition']['steps']:
                phase = 'act'; observations = 0
                while True:
                    check()
                    path, screen = capture()
                    material = {'step':step, 'phase':phase, 'screen':screen, 'history':history[-12:],
                                'baseline':baseline.get('snapshot') if baseline else None,
                                'baseline_observations':[{k:e.get(k) for k in ('id','expected','actual','sha256')}
                                    for e in baseline_evidence if e['status'] == 'pass' and
                                    e.get('details', {}).get('chain_id') == snapshot['chain_id'] and
                                    e.get('details', {}).get('step_id') == step['id']]}
                    try:
                        verdict = judge(request, material, [str(path)])
                    except TimeoutError as exc:
                        check()
                        raise RuntimeError('模型判断超时，需恢复模型服务后重新运行') from exc
                    except Exception:
                        check()
                        raise
                    check()
                    from .usage import trace_tokens
                    trace = Path(verdict.get('evidence', ''))/'trace.jsonl'
                    used = trace_tokens(trace) if trace.is_file() else 0
                    tokens += used
                    update({'tokens':tokens, 'next_observation_at':None})
                    decision = verdict.get('decision'); actual = str(verdict.get('actual', '')).strip()
                    if decision not in ('act','observe','pass','fail','blocked') or not actual:
                        raise RuntimeError('模型缺少有效判断及截图观察结果')
                    evidence = save(snapshot, step, '观察当前界面', actual,
                        decision if decision in ('pass','fail','blocked') else 'recorded', path,
                        {'phase':phase, 'model':verdict.get('model'), 'model_evidence':verdict.get('evidence'), 'tokens':used})
                    history.append({'goal':step['goal'], 'actual':actual, 'decision':decision})
                    if decision == 'pass':
                        break
                    if decision in ('fail','blocked'):
                        status, reason = decision, step['goal'] + '：' + actual
                        return {'status':status, 'reason':reason, 'test_chain_run_id':run['id']}
                    if phase == 'observe' and decision == 'act':
                        raise RuntimeError('持续观测阶段禁止重复执行操作')
                    if decision == 'observe':
                        if not step.get('loop'):
                            raise RuntimeError('该检查点未启用 Loop，无法继续等待')
                        phase = 'observe'; observations += 1
                        if observations >= config['max_observations']:
                            raise TimeoutError('观测达到最大轮数')
                        update({'observations':observations, 'next_observation_at':time.time()+config['interval_seconds']}, 'observing')
                        deadline = clock()+config['interval_seconds']
                        while clock() < deadline:
                            check(); sleep(min(.5, max(0, deadline-clock())))
                        if step.get('observation_action') in ('reload', 'navigate'):
                            if actions >= config['max_actions']:
                                raise TimeoutError('达到最大动作数')
                            observation = {'type':'navigate', 'url':step['observation_url']} if step['observation_action'] == 'navigate' else {'type':'reload'}
                            driver.act(observation); actions += 1
                            after, _ = capture()
                            save(snapshot, step, json.dumps(observation, ensure_ascii=False), '已打开观测页面', 'recorded', after, {'before_evidence_id':evidence['id']})
                        continue
                    action = verdict.get('action', {})
                    if action.get('type') not in ('click','type','key','scroll','navigate','wait','reload'):
                        raise RuntimeError('模型动作不受支持')
                    if actions >= config['max_actions']:
                        raise TimeoutError('达到最大动作数')
                    driver.act(action); actions += 1
                    # Small rendering delay is bounded and never replays an action.
                    sleep(.2)
                    after, _ = capture()
                    save(snapshot, step, json.dumps(action, ensure_ascii=False), '动作完成，等待下一次截图判断', 'recorded', after,
                         {'before_evidence_id':evidence['id']})
                    history.append({'action':action})
        if request.get('verify_workspace_identity'):
            from .local_testing import identity
            identity(request)
        status, reason = 'pass', '全部必要链路与预期均通过'
    except InterruptedError as exc:
        status, reason = ('cancelled' if ledger.get('test_chain_runs', run['id']).get('cancel_requested') else 'blocked'), str(exc)
    except TimeoutError as exc:
        status, reason = 'timeout', str(exc)
    except Exception as exc:
        status, reason = 'blocked', str(exc)
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
        try:
            driver.close()
        except Exception:
            pass
        recorder.finish(status, reason)
        update({'reason':reason, 'finished_at':time.time(), 'evidence_ids':evidence_ids, 'elapsed':clock()-start}, status)
    return {'status':status, 'reason':reason, 'test_chain_run_id':run['id']}
