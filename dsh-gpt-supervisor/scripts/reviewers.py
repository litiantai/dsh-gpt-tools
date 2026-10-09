"""Reviewer configuration, model catalogs and process adapters."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import threading
import time

PROVIDERS = ('codex', 'claude', 'harness')
PHASES = ('plan', 'checkpoint', 'acceptance')
NAMES = {'codex': 'Codex/GPT', 'claude': 'Claude CLI', 'harness': 'DeepSeek Harness'}
CATALOG_LOCK = threading.Lock()


def normalize(cfg):
    cfg = copy.deepcopy(cfg)
    cfg.setdefault('reviewer_mode', 'unified')
    cfg.setdefault('reviewer_unified', {'provider': 'codex', 'model': cfg['model']})
    cfg.setdefault('reviewer_stages', {})
    cfg.setdefault('claude_bin', shutil.which('claude') or 'claude')
    cfg.setdefault('harness_bin', shutil.which('dsh') or 'dsh')
    cfg.setdefault('harness_profile', 'supervisor-review')
    cfg.setdefault('node_bin', shutil.which('node') or 'node')
    cfg.setdefault('reviewer_helper', str(Path(__file__).with_name('reviewer_cli.mjs')))
    return cfg


def selection(value):
    if not isinstance(value, dict) or value.get('provider') not in PROVIDERS:
        raise ValueError('请选择有效审查器')
    if not isinstance(value.get('model'), str) or not value['model'].strip():
        raise ValueError('请选择审查模型')
    result = {'provider': value['provider'], 'model': value['model'].strip()}
    if result['provider'] == 'harness':
        if not isinstance(value.get('model_provider'), str) or not value['model_provider'].strip():
            raise ValueError('Harness 模型必须包含提供商')
        result['model_provider'] = value['model_provider'].strip()
    return result


def validate_config(cfg):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', cfg['harness_profile']) or cfg['harness_profile'] in ('web','desktop','tui'):
        raise ValueError('Harness 审查必须使用独立 profile 名称')
    if cfg['reviewer_mode'] not in ('unified', 'stages'):
        raise ValueError('无效审查配置模式')
    cfg['reviewer_unified'] = selection(cfg['reviewer_unified'])
    stages = cfg['reviewer_stages']
    if not isinstance(stages, dict) or set(stages) - set(PHASES):
        raise ValueError('无效阶段配置')
    cfg['reviewer_stages'] = {p: selection(v) for p, v in stages.items()}
    if cfg['reviewer_mode'] == 'stages' and set(stages) != set(PHASES):
        raise ValueError('请配置全部三个审查阶段')
    return cfg


def snapshot(cfg, phase):
    cfg = normalize(cfg)
    selected = selection(cfg['reviewer_unified'] if cfg['reviewer_mode'] == 'unified' else cfg['reviewer_stages'][phase])
    return selected | {'bin': cfg[selected['provider'] + '_bin'], 'home': cfg['home'], 'harness_home': cfg.get('harness_home', cfg['home']),
                       'harness_profile': cfg['harness_profile'], 'review_timeout': cfg['review_timeout']}


def harness_command(selected, dest, catalog=False):
    # A dedicated base + headless profile is provisioned at installation. Never use the web profile.
    if selected['harness_profile'] in ('web', 'desktop', 'tui'):
        raise ValueError('Harness 审查必须使用独立 profile')
    guard = Path(__file__).with_name('harness-reviewer.mjs')
    patch = [
        {'id': 'dsh-supervisor-connector', 'disabled': True},
        {'id': 'dsh-supervisor-connector-modern', 'disabled': True},
        {'id': 'hmr', 'disabled': True},
        {'id': 'session-persistence-jsonl', 'config': {'root': str(dest / 'sessions')}},
        {'insert': [{'id': 'supervisor-review-guard', 'name': str(guard)}]},
    ]
    if catalog:
        patch += [{'id': 'headless-runner', 'disabled': True}, {'id': 'headless-startup', 'disabled': True}]
    else:
        patch += [{'id': 'agent-default-model', 'config': {'provider': selected['model_provider'], 'model': selected['model']}}]
    overlay = dest / 'reviewer.patch.json'
    overlay.write_text(json.dumps(patch))
    env = dict(os.environ, DSH_HOME=selected.get('harness_home', selected['home']), DSH_SUPERVISOR_REVIEW='catalog' if catalog else 'run')
    cmd = [selected['bin'], '--profile', selected['harness_profile'], '--patch', str(overlay)]
    if not catalog:
        cmd += ['--json', '-']
    return cmd, env


def command(selected, dest, packet):
    provider = selected['provider']
    if provider == 'codex':
        return [selected['bin'], 'exec', '-m', selected['model'], '--ephemeral', '--skip-git-repo-check',
                '--sandbox', 'workspace-write', '-C', packet['cwd'], '--output-schema', str(dest/'schema.json'),
                '--json', '-o', str(dest/'last.json'), '-c', 'model_reasoning_effort="low"', '-c', 'notify=[]', '-'], None
    if provider == 'claude':
        return [selected['bin'], '-p', '--model', selected['model'], '--output-format', 'json',
                '--json-schema', (dest/'schema.json').read_text(), '--no-session-persistence',
                '--permission-mode', 'dontAsk', '--tools', 'Read,Glob,Grep,Bash',
                '--allowedTools', 'Read,Glob,Grep,Bash', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--safe-mode'], None
    return harness_command(selected, dest)


def read_result(selected, dest):
    if selected['provider'] == 'codex':
        return json.loads((dest/'last.json').read_text())
    text = (dest/'trace.jsonl').read_text()
    if selected['provider'] == 'claude':
        envelope = json.loads(text)
        if envelope.get('is_error') or envelope.get('permission_denials'):
            raise ValueError('Claude 审查失败或工具权限不足')
        result = envelope.get('structured_output')
        if not isinstance(result, dict):
            raise ValueError('Claude 未返回结构化审查结果')
    else:
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        if any(e.get('type') == 'error' for e in events):
            raise ValueError('Harness 审查执行失败')
        finals = [e for e in events if e.get('type') == 'final']
        if len(finals) != 1:
            raise ValueError('Harness 未返回唯一最终结果')
        answer = finals[0].get('text', '')
        if answer.startswith('```json\n') and answer.endswith('\n```'):
            answer = answer[8:-4]
        result = json.loads(answer)
    (dest/'last.json').write_text(json.dumps(result, ensure_ascii=False))
    return result


def catalog(cfg, state, provider, refresh=False):
    from review_core import stop_review_process
    if provider not in PROVIDERS:
        raise ValueError('未知审查器')
    cfg = normalize(cfg)
    identity = {k: cfg[k] for k in (provider+'_bin', 'home', 'harness_profile', 'node_bin', 'reviewer_helper')}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    dest = Path(state)/'model-catalogs'/provider/key
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = dest/'catalog.json'
    with CATALOG_LOCK:
        try:
            previous = json.loads(cache.read_text()) if cache.exists() else None
        except (ValueError, OSError):
            previous = None
        if previous and not refresh and time.time() - previous['fetched_at'] < 300:
            return previous
        proc = None
        try:
            if provider == 'harness':
                cmd, env = harness_command({'bin':cfg['harness_bin'],'home':cfg['home'],'harness_profile':cfg['harness_profile']},dest,True)
                prompt = ''
            else:
                cmd, env = [cfg['node_bin'], cfg['reviewer_helper']], None
                prompt = json.dumps({'provider':provider,'bin':cfg[provider+'_bin']})
            with (dest/'stderr.log').open('w') as err:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                        text=True, start_new_session=True, env=env, cwd=dest)
                out, _ = proc.communicate(prompt, timeout=25)
            if proc.returncode:
                raise ValueError(f'{NAMES[provider]} 模型目录查询失败（退出码 {proc.returncode}）')
            rows = json.loads(out)
            if not isinstance(rows, list) or not rows:
                raise ValueError('工具未返回可用模型')
            models = []
            for row in rows:
                if not isinstance(row.get('id'), str) or not row['id']:
                    raise ValueError('工具返回无效模型 ID')
                models.append({k:v for k,v in row.items() if k in ('id','name','model_provider','provider_name','description')})
            value = {'provider':provider,'models':models,'fetched_at':time.time(),'source':NAMES[provider], 'stale':False,'error':None}
            from review_core import atomic_json
            atomic_json(cache, value)
            return value
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return (previous or {'provider':provider,'models':[],'fetched_at':None,'source':NAMES[provider]}) | {'stale':bool(previous),'error':str(exc)}
        finally:
            if proc is not None and not stop_review_process(proc):
                raise RuntimeError('模型目录进程未确认停止')
