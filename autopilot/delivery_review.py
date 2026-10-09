"""固定 OCR 规则、可选模型与独立修复；每轮保存实际模型及提交证据。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from .call import atomic
from .delivery_worker import ROOT, paths, journal, commit_source, network_git
from .workspace import git, digest
from .sandbox import restrict
from .github import load_pull_request, credential_file


def agent_error(result):
    """识别运行异常，兼容旧回执；不把代码问题归为 Agent 故障。"""
    reason = result.get('reason', '')
    return result.get('failure_kind') == 'agent_execution' or result.get('status') == 'blocked' and any(
        text in reason for text in ('模型执行失败', 'Harness 执行失败', 'Harness 审查执行失败',
            '最终回执解析失败', '执行进程已失联', '执行超时'))


def publish_result(gh, number, round_id, folder, result, state, description):
    """先保存完整回执，再发布报告及检查链接；网络错误不能覆盖评审结论。"""
    atomic(folder / 'result.json', result)
    try:
        result['report_url'] = gh.review_report(number, round_id, result)['html_url']
    except (ValueError, OSError) as exc:
        result['reporting_error'] = str(exc)
    atomic(folder / 'result.json', result)
    try:
        gh.status(result['head_sha'], state, description, target_url=result.get('report_url'))
    except (ValueError, OSError) as exc:
        result['reporting_error'] = str(exc)
        atomic(folder / 'result.json', result)
    return result


def schema(review=False):
    text = {'type': 'string'}
    fields = {'status': {'type': 'string', 'enum': ['pass', 'fail', 'blocked']}, 'summary': text, 'reason': text}
    if review:
        fields['coverage'] = {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'path': text, 'status': {'type': 'string', 'enum': ['reviewed', 'skipped']}, 'reason': text}, 'required': ['path', 'status', 'reason']}}
        fields['issues'] = {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'path': text, 'severity': {'type': 'string', 'enum': ['critical', 'high', 'medium', 'low']},
                'content': text, 'start_line': {'type': 'integer'}}, 'required': ['path', 'severity', 'content', 'start_line']}}
    return {'type': 'object', 'additionalProperties': False, 'properties': fields, 'required': list(fields)}


def model(request, folder, prompt, review=False, write=False):
    from review_core import Store
    from reviewers import normalize, snapshot, command, read_result
    from .store import Ledger
    from .usage import register
    selected = request['record']['selection']
    state = Path(request['state_root']).parent
    store = Store(state)
    cfg = normalize(store.settings())
    cfg.update(reviewer_mode='unified', reviewer_unified=selected)
    actual = snapshot(cfg, 'acceptance') | {'reasoning_effort': selected.get('reasoning_effort', 'medium') if selected['provider'] == 'codex' else None}
    if selected.get('bin'):
        actual['bin'] = selected['bin']
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _, _, _, workspace = paths(request)
    atomic(folder / 'schema.json', schema(review))
    atomic(folder / 'selection.json', {k: v for k, v in actual.items() if k not in ('home', 'harness_home')})
    register(Ledger(store), request['product']['id'], folder / 'trace.jsonl', budget_kind='code_delivery_tokens')
    if actual['provider'] == 'harness' and (write or not review):
        from .executor import execute
        product = copy.deepcopy(request['product'])
        product['repository'] = str(paths(request)[2])
        product.setdefault('agents', {})['implementation'] = selected
        worker = request['record'] | {'workspace': str(workspace), 'worker_home': str(folder / 'home'), 'plan': prompt}
        result = execute('develop' if write else 'plan', request | {'product': product, 'record': worker, 'requirement': {'title': '修复评审问题', 'acceptance': [prompt]}})
    else:
        if actual['provider'] == 'harness':
            # Harness composes its profile at startup. Keep those writes inside
            # this round, never grant the model write access to the shared home.
            home = folder / 'home'
            subprocess.run([request['product'].get('node', 'node'), str(ROOT / 'scripts/autopilot-profile.mjs'),
                actual['harness_home'], actual['harness_profile'], str(home), request['product']['worker_runtime'],
                str(ROOT / 'scripts/autopilot-guard.mjs')], check=True, capture_output=True)
            actual |= {'harness_home': str(home), 'harness_profile': 'autopilot-review'}
        argv, env = command(actual, folder, {'cwd': str(workspace)})
        if actual['provider'] == 'harness':
            overlay = folder / 'reviewer.patch.json'
            atomic(overlay, json.loads(overlay.read_text()) + [{'id': 'sandbox-policy', 'config': {'mode': 'danger-full-access'}}])
        if actual['provider'] == 'codex':
            argv[argv.index('model_reasoning_effort="low"')] = 'model_reasoning_effort=' + json.dumps(actual['reasoning_effort'])
            # macOS does not allow Codex to apply a second Seatbelt profile.
            # The outer profile below remains the mandatory write boundary.
            argv[argv.index('--sandbox') + 1] = 'danger-full-access'
            argv[2:2] = ['--ignore-user-config', '--ignore-rules', '-c', 'approval_policy="never"']
        if actual['provider'] == 'claude' and write:
            argv = [v.replace('Read,Glob,Grep,Bash', 'Read,Glob,Grep,Bash,Write,Edit') for v in argv]
        allowed = [folder, Path(tempfile.gettempdir()), Path.home() / '.codex']
        if write:
            allowed.append(workspace)
        # Git object/index writes are reserved for the controller; model edits only source.
        argv = restrict(argv, allowed, folder / 'agent.sb', private_roots=[credential_file().parent], deny_local=True)
        env = (env or os.environ.copy()) | {'GIT_OPTIONAL_LOCKS': '0'}
        with (folder / 'trace.jsonl').open('w') as out, (folder / 'stderr.log').open('w') as err:
            proc = subprocess.run(argv, input=prompt + '\n输出必须符合此 JSON schema：\n' + json.dumps(schema(review)),
                text=True, cwd=workspace, stdout=out, stderr=err, env=env, timeout=3600)
        if proc.returncode:
            from .store import redact
            result = {'status': 'blocked', 'reason': f"{actual['provider']} 模型执行失败，退出码 {proc.returncode}",
                'failure_kind': 'agent_execution',
                'detail': redact((folder / 'stderr.log').read_text(errors='replace')[-4000:]), 'exit_code': proc.returncode}
        else:
            try:
                result = read_result(actual, folder)
            except (ValueError, OSError) as exc:
                result = {'status': 'blocked', 'failure_kind': 'agent_execution', 'reason': 'Agent 回执读取失败：' + str(exc)}
    if agent_error(result):
        result['failure_kind'] = 'agent_execution'
    result.update(provider=actual['provider'], model=actual['model'], reasoning_effort=actual['reasoning_effort'], evidence=result.get('evidence') or str(folder))
    atomic(folder / 'result.json', result)
    return result


def validate_coverage(result, expected):
    coverage = result.get('coverage')
    issues = result.get('issues')
    if not isinstance(coverage, list) or not isinstance(issues, list):
        raise ValueError('评审缺少逐文件覆盖或问题列表')
    paths_seen = [r.get('path') for r in coverage]
    if sorted(paths_seen) != sorted(expected) or any(r.get('status') != 'reviewed' for r in coverage):
        raise ValueError('评审存在漏审、重复文件或跳过文件，不能通过')
    if any(r.get('severity') not in ('critical', 'high', 'medium', 'low') for r in issues):
        raise ValueError('评审问题缺少有效严重程度')
    return result.get('status') == 'pass' and not any(r['severity'] in ('critical', 'high', 'medium') for r in issues)


def verify(request, workspace, head, base, folder):
    product, batch = request['product'], request['record']
    requirements = request.get('requirements', [])
    combined = {'title': batch['title'], 'acceptance': [a for r in requirements for a in r.get('acceptance', [])],
                'resolution_probes': [p for r in requirements for p in r.get('resolution_probes', [])]}
    # 基线/首次源码交付没有已合入业务需求：以导入基线摘要与已登记 acceptance_checks
    # 作为可追溯的验收条件，既不虚构需求，也不让验证者在空条件下启动。
    baseline_digest = batch.get('baseline_source_digest')
    registered_checks = list(((product.get('project_config') or {}).get('acceptance_checks') or {}).keys())
    baseline_acceptance = False
    if not combined['acceptance'] and not combined['resolution_probes'] and baseline_digest:
        combined['acceptance'] = ['隔离工作区源码摘要等于导入基线 ' + str(baseline_digest)]
        combined['resolution_probes'] = [{'path': 'check:' + name, 'pointer': '/status',
            'operator': 'equals', 'expected': 'pass'} for name in registered_checks]
        baseline_acceptance = True
    if not combined['acceptance'] and not combined['resolution_probes']:
        # 两类来源都为空时保持“证据不足即 blocked”，绝不伪造通过。
        result = {'status': 'blocked', 'reason': '缺少业务需求验收条件与已登记 acceptance_checks，独立验证无从核对'}
        atomic(folder / 'verification.json', result)
        return result
    actual_digest = None
    if baseline_acceptance:
        # 控制器在未沙箱化侧自算工作区摘要并与导入基线比对：不一致立即 blocked，
        # 不启动验证适配器，避免验证者因无法读取 git 元数据而误判。
        try:
            actual_digest = digest(workspace)
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            detail = '无法计算工作区源码摘要：' + str(exc)
            result = {'status': 'blocked', 'reason': detail, 'baseline_source_digest': baseline_digest,
                'verification': {'status': 'blocked', 'reason': detail, 'baseline_source_digest': baseline_digest}}
            atomic(folder / 'verification.json', result)
            return result
        if actual_digest != baseline_digest:
            result = {'status': 'blocked', 'reason': '工作区源码摘要与导入基线不一致，不能以基线验收条件启动独立验证',
                'source_digest': actual_digest, 'baseline_source_digest': baseline_digest,
                'verification': {'status': 'blocked', 'reason': '工作区源码摘要与导入基线不一致',
                    'source_digest': actual_digest, 'baseline_source_digest': baseline_digest}}
            atomic(folder / 'verification.json', result)
            return result
    record = batch | {'id': str(uuid.uuid4()), 'workspace': str(workspace), 'commit': head, 'base_commit': base,
        'summary': '交付整合后完整验证；核对全部关联业务验收条件'}
    if baseline_acceptance:
        record['source_digest'] = actual_digest
        record['baseline_source_digest'] = baseline_digest
    if not (record.get('repository') and record.get('git_dir')):
        # 交付流程显式传入 bare 仓库；缺失时按工作区元数据补一个可用 git 来源。
        try:
            from .workspace import metadata
            _, common = metadata(workspace)
            record.setdefault('repository', str(common)); record.setdefault('git_dir', str(common))
        except (subprocess.CalledProcessError, OSError, ValueError):
            pass
    argv = product.get('adapter')
    if not argv:
        return {'status': 'blocked', 'reason': '项目缺少必需验证适配器'}
    from .codex_executor import verification_material
    try:
        # 在未沙箱化的控制器侧落盘 base..commit 差异，作为独立验证的自包含事实证据。
        verification = verification_material(workspace, record, folder)
    except RuntimeError as exc:
        # 差异证据是硬前置：缺失时不得在无差异、无验收条件的情况下启动验证模型。
        result = {'status': 'blocked', 'reason': '独立验证缺少自包含差异证据：' + str(exc),
            'verification': {'status': 'blocked', 'reason': str(exc)}}
        atomic(folder / 'verification.json', result)
        return result
    if baseline_acceptance:
        verification['baseline_acceptance'] = True
        verification['acceptance_checks'] = registered_checks
        verification['source_digest'] = actual_digest
        verification['baseline_source_digest'] = baseline_digest
        verification['baseline_digest_verified'] = True
        facts_path = Path(verification.get('facts_file') or '')
        if facts_path.is_file():
            # 证据自包含：facts 文件与 verification 保持一致，离线即可复核摘要核对结论。
            facts = json.loads(facts_path.read_text(encoding='utf-8'))
            facts.update({'baseline_acceptance': True, 'acceptance_checks': registered_checks,
                'source_digest': actual_digest, 'baseline_source_digest': baseline_digest,
                'baseline_digest_verified': True})
            facts_path.write_text(json.dumps(facts, ensure_ascii=False), encoding='utf-8')
    payload = request | {'record': record, 'requirement': combined, 'verification': verification}
    proc = subprocess.run(argv + ['verify'], input=json.dumps(payload),
        text=True, capture_output=True, timeout=7200)
    (folder / 'verification.stderr.log').write_text(proc.stderr)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        return {'status': 'blocked', 'reason': '验证适配器未返回结构化结果'}
    diff_path = Path(verification['diff_file'])
    diff_hash = hashlib.sha256(diff_path.read_bytes()).hexdigest() if diff_path.is_file() else ''
    for check in result.get('checks', []):
        if check.get('name') == '独立业务验证' and isinstance(check.get('evidence'), dict):
            check['evidence'] |= {'diff_file': verification['diff_file'], 'facts_file': verification['facts_file'],
                'diff_sha256': diff_hash, 'changed_files': verification['changed_files'],
                'merge_base': verification['merge_base'], 'source': verification['source']}
    result['verification'] = {key: value for key, value in verification.items() if key != 'diff'}
    atomic(folder / 'verification.json', result)
    if proc.returncode or not result.get('checks') or any(c.get('status') != 'pass' for c in result['checks'] if c.get('required', True)):
        return {'status': 'fail', 'reason': '交付必需检查未通过', 'checks': result.get('checks', [])}
    return result


def execute(action, request):
    request = request | {'budget_kind': 'code_delivery_tokens'}
    record = request['record']
    _, batch, repo, workspace = paths(request)
    folder = batch / 'rounds' / (record.get('round_id') or ('validation-' + str(uuid.uuid4())))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    pr_context = None
    if action in ('review', 'validate') or record.get('pr_url'):
        gh, pr = load_pull_request(record)
        if pr.get('state') != 'open' or pr.get('merged'):
            return {'status': 'blocked', 'reason': '已保存的 PR 已关闭或合并，不能继续评审或修复', 'pr_url': record['pr_url']}
        pr_context = {'url': record['pr_url'], 'number': pr['number'], 'title': pr.get('title'),
            'description': pr.get('body'), 'base_sha': pr['base']['sha'], 'head_sha': pr['head']['sha']}
        atomic(folder / 'pull-request.json', pr_context)
        changed = pr['head']['sha'] != record.get('head_sha') or pr['base']['sha'] != record.get('base_sha')
        if changed and action in ('review', 'validate'):
            return {'status': 'stale', 'stale': True, 'reason': 'PR 提交已变化，需要同步后评审', 'pr_url': record['pr_url']}
        if action == 'repair' and pr['head']['sha'] != record.get('head_sha'):
            return {'status': 'stale', 'stale': True, 'reason': '修复前 PR 提交已变化，保留隔离工作区并重新同步评审', 'pr_url': record['pr_url']}
    if action == 'repair':
        material = json.dumps({'pull_request': pr_context, 'requirements': request.get('requirements'), 'feedback': record.get('feedback'),
            'conflicts': git(workspace, 'diff', '--name-only', '--diff-filter=U').splitlines()}, ensure_ascii=False)
        plan_file = folder / 'repair-plan.json'
        if plan_file.exists():
            plan = json.loads(plan_file.read_text())
        else:
            plan = model(request, folder / 'plan', '只读分析并给出修复或冲突解决方案，不修改文件，不提交、不推送。保留双方有效业务逻辑。'
                '本阶段判断的是修复方案是否就绪：能给出可执行方案即返回 pass，并将具体步骤写入 summary；'
                '现有代码仍有问题不代表方案 fail。缺少必要前提时返回 blocked 并解释原因。\n' + material)
            atomic(plan_file, plan)
        if plan.get('status') != 'pass':
            return plan
        result = model(request, folder / 'fix', '按以下方案修复隔离工作区源码；禁止提交、推送、修改其他工作区或正式应用。'
            '完成后运行相关测试；冲突文件移除冲突标记，由控制器暂存和提交。\n' + json.dumps(plan, ensure_ascii=False) + '\n' + material, write=True)
        if result.get('status') != 'pass':
            return result
        state = journal(batch / 'journal.json')
        # Require source edits; a claim of success alone cannot resolve the loop.
        if not git(workspace, 'status', '--porcelain'):
            return {'status': 'blocked', 'reason': '修复模型没有产生修改，保留原问题等待处理', 'plan': plan}
        for name in git(workspace, 'diff', '--name-only', '--diff-filter=U').splitlines():
            path = workspace / name
            if path.is_file() and any(line.startswith(('<<<<<<< ', '=======', '>>>>>>> ')) for line in path.read_text(errors='replace').splitlines()):
                return {'status': 'fail', 'reason': '修复后仍有冲突标记', 'plan': plan}
        marker = 'Delivery-Run: ' + state['pending_run'] if state.get('pending_run') else 'Resolve delivery review findings'
        head = commit_source(workspace, marker)
        if state.get('pending_run'):
            atomic(batch / 'journal.json', state | {'integrated_ids': list(dict.fromkeys(state.get('integrated_ids', []) + [state['pending_run']])), 'pending_run': None})
        return result | {'status': 'pass', 'head_sha': head, 'plan': plan}
    if action not in ('review', 'validate'):
        raise ValueError('未知交付评审阶段')
    head, base = git(workspace, 'rev-parse', 'HEAD'), record['base_sha']
    if head != record['head_sha'] or git(workspace, 'status', '--porcelain'):
        raise ValueError('评审工作区版本不一致或存在未提交改动')
    before = digest(workspace)
    if action == 'validate':
        proof = record.get('review_pass') or {}
        if proof.get('head_sha') != head or proof.get('base_sha') != base:
            return {'status': 'stale', 'stale': True, 'reason': '运行验证缺少当前版本的代码评审通过证据'}
        gh.status(head, 'pending', '代码评审已通过，正在执行合并前运行验证')
        checked = verify(request | {'record': record | {'repository': str(repo), 'git_dir': str(repo)}},
                         workspace, head, base, folder)
        if checked.get('status') != 'pass':
            gh.status(head, 'failure', '交付验证：' + checked.get('reason', '必需检查未通过'))
            return checked
        _, current = load_pull_request(record)
        if digest(workspace) != before or git(workspace, 'rev-parse', 'HEAD') != head or current.get('state') != 'open' or current['head']['sha'] != head or current['base']['sha'] != base:
            return {'status': 'stale', 'stale': True, 'reason': '运行验证期间源码或 PR 变化，结论失效'}
        gh.status(head, 'success', '独立代码评审及合并前验证均通过')
        return checked | {'head_sha': head, 'base_sha': base, 'pr_url': record['pr_url']}
    lock = json.loads((ROOT / 'vendor/open-code-review/lock.json').read_text())
    skill = (ROOT / 'vendor/open-code-review/SKILL.md').read_bytes()
    if hashlib.sha256(skill).hexdigest() != lock['skill_sha256']:
        raise ValueError('OCR 技能与锁定版本不一致')
    ocr = ROOT / 'node_modules/.bin/ocr'
    installed = json.loads((ROOT / 'node_modules/@alibaba-group/open-code-review/package.json').read_text())
    if installed['version'] != lock['cli_version']:
        raise ValueError('OCR CLI 与锁定版本不一致，请安装项目锁定依赖')
    def invoke(*args):
        out = subprocess.check_output([str(ocr), 'delegate', *args], cwd=workspace, text=True, timeout=120)
        return json.loads(out)
    diff_base = git(workspace, 'merge-base', base, head)
    preview = invoke('preview', '--format', 'json', '--from', diff_base, '--to', head)
    atomic(folder / 'ocr-preview.json', preview)
    files = preview.get('reviewable_files')
    if not isinstance(files, list):
        raise ValueError('OCR 文件目录格式不兼容，不能跳过覆盖检查')
    expected = [r['path'] for r in files]
    rules = invoke('rule', '--format', 'json', *expected) if expected else []
    atomic(folder / 'ocr-rules.json', rules)
    context = {'pull_request': pr_context, 'requirements': request.get('requirements'), 'goal': request['product']['goal'],
               'base_sha': base, 'head_sha': head, 'diff_base_sha': diff_base, 'files': files, 'rules_file': str(folder / 'ocr-rules.json'),
               'verification': {'status': 'pending', 'reason': '本轮只执行代码评审；通过后由控制器执行运行验证，不启动客户端'}}
    prompt = '你是独立代码评审 Agent，只读检查源码，不修改、不提交、不推送。以下官方技能用于逐文件评审；必须覆盖全部给定文件，不能因发现问题提前结束。\n'
    prompt += skill.decode() + '\n本轮确定的范围和证据：\n' + json.dumps(context, ensure_ascii=False)
    gh.status(head, 'pending', '独立模型正在评审 PR 全部增量，尚未启动运行验证')
    result = model(request, folder / 'review', prompt, review=True)
    if result.get('status') == 'blocked':
        result |= {'head_sha': head, 'base_sha': base, 'pr_url': record['pr_url'], 'total_files': len(expected)}
        return publish_result(gh, pr_context['number'], record.get('round_id') or folder.name, folder, result,
            'error', ('评审 Agent 运行异常：' if agent_error(result) else '代码评审阻塞：') + result.get('reason', '未形成有效评审结论'))
    if digest(workspace) != before or git(workspace, 'rev-parse', 'HEAD') != head:
        raise ValueError('验证或评审期间源码变化，结论无效')
    _, current = load_pull_request(record)
    if current.get('state') != 'open' or current['head']['sha'] != head or current['base']['sha'] != base:
        return {'status': 'stale', 'stale': True, 'reason': '评审期间 PR 发生变化，结论失效', 'pr_url': record['pr_url']}
    passed = validate_coverage(result, expected)
    result = result | {'status': 'pass' if passed else 'fail', 'head_sha': head, 'base_sha': base, 'diff_base_sha': diff_base, 'pr_url': record['pr_url'],
        'skill_version': lock, 'total_files': len(expected), 'reviewed_files': len(expected),
        'reason': '' if passed else result.get('reason') or result.get('summary') or '存在中高危问题或评审未通过'}
    return publish_result(gh, pr_context['number'], record.get('round_id') or folder.name, folder, result,
        'pending' if passed else 'failure', '代码评审已通过，等待合并前运行验证' if passed else '代码评审未通过：' + result['reason'])
