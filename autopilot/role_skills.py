"""跨执行器角色规则加载、内容快照和输出契约。"""
from contextvars import ContextVar
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'skills'
READS = ContextVar('role_skill_reads', default=())
ROLES = {
    'coordinate': 'coordinator', 'discover': 'discovery', 'investigate': 'discovery',
    'daily_attribution': 'discovery', 'chat': 'clarification', 'plan': 'implementation',
    'develop': 'implementation', 'repair': 'implementation', 'validate': 'verification',
    'verify': 'verification', 'computer_generate': 'verification', 'computer_step': 'verification',
    'daily_acceptance': 'verification', 'acceptance': 'acceptance', 'plan_review': 'acceptance',
    'acceptance_review': 'acceptance', 'collaborate': 'collaboration',
    'find_competitors': 'research', 'analyze_competitors': 'research',
    'review': 'code-review', 'daily_retrospective': 'retrospective',
}


def read(relative):
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('角色技能资源必须使用包内相对路径')
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
        raise ValueError('角色技能资源缺失或路径无效：' + str(relative))
    content = path.read_text(encoding='utf-8')
    READS.set(READS.get() + ((str(relative), content),))
    return content


def rule(name):
    index = json.loads((ROOT / 'roles.json').read_text())
    if name not in index:
        raise ValueError('角色规则未登记：' + name)
    return read(index[name])


def output_schema(action):
    return json.loads(read(f'dsh-role-{ROLES[action]}/schemas/{action}.json'))


def bind(action, folder, selection, instruction, schema=None, *, config=None, workspace=None):
    """保存实际规则正文和契约；历史调用不受后续发布影响。"""
    role = ROLES.get(action)
    if role is None:
        raise ValueError('执行阶段没有角色技能：' + action)
    from .role_workflows import load, instruction as workflow_instruction, sha
    snapshot = Path(folder) / 'role-skill'
    binding_hash = sha({'instruction': instruction, 'schema': schema,
                        'selection': {k: selection.get(k) for k in ('provider', 'model', 'reasoning_effort')},
                        'config': config})
    if (snapshot / 'manifest.json').exists():
        existing = json.loads((snapshot / 'manifest.json').read_text())
        if 'binding_hash' not in existing:
            # Pre-workflow calls retain their original entrypoint and contract.
            # Do not inject new workflow rules into an already frozen call.
            saved_entry = (snapshot / f'dsh-role-{role}/SKILL.md').read_text()
            legacy_prompt = saved_entry + '\n\n本次阶段规则：\n' + instruction
            matches = (hashlib.sha256(legacy_prompt.encode()).hexdigest() == existing['instruction_sha256']
                       and hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() == existing['schema_sha256']
                       and existing['selection'] == {k: selection.get(k) for k in ('provider', 'model', 'reasoning_effort')})
        else:
            matches = existing['binding_hash'] == binding_hash
        if existing:
            if not matches:
                raise ValueError('调用已有不同的角色技能快照，请建立新调用')
            for resource in existing['files'] + existing.get('resources', []):
                path = (snapshot / resource['path']).resolve()
                if not path.is_relative_to(snapshot.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != resource['sha256']:
                    raise ValueError('角色技能快照已被修改')
            prompt = (snapshot / 'instructions.md').read_text()
            if hashlib.sha256(prompt.encode()).hexdigest() != existing['instruction_sha256']:
                raise ValueError('角色指令快照已被修改')
            READS.set(())
            return prompt + '\n技能支持文件快照目录：' + str(snapshot / f'dsh-role-{role}'), existing
    workflow = load(role, action)
    if workflow:
        instruction += '\n\n' + workflow_instruction(workflow, config, workspace)
    entry = read(f'dsh-role-{role}/SKILL.md')
    if not entry.startswith('---\n') or 'name: dsh-role-' + role not in entry:
        raise ValueError('角色技能元数据无效')
    # Imported shared rules are also retained, even if imported before this invocation.
    index = json.loads((ROOT / 'roles.json').read_text())
    for name, relative in index.items():
        if name.startswith('acceptance_scope-') and rule(name) in instruction:
            read(relative)
    files = dict(READS.get())
    READS.set(())
    folder = Path(folder)
    snapshot = folder / 'role-skill'
    prompt = entry + '\n\n本次阶段规则：\n' + instruction
    instruction_hash = hashlib.sha256(prompt.encode()).hexdigest()
    schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    if (snapshot / 'manifest.json').exists():
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        model = {k: selection.get(k) for k in ('provider', 'model', 'reasoning_effort')}
        if (manifest['instruction_sha256'] != instruction_hash or manifest['schema_sha256'] != schema_hash
                or manifest['selection'] != model):
            raise ValueError('调用已有不同的角色技能快照，请建立新调用')
        return (snapshot / 'instructions.md').read_text() + '\n技能支持文件快照目录：' + str(snapshot / f'dsh-role-{role}'), manifest
    snapshot.mkdir(parents=True, exist_ok=True)
    entries = []
    for relative, content in sorted(files.items()):
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        entries.append({'path': relative, 'sha256': hashlib.sha256(content.encode()).hexdigest()})
    resources = []
    # Keep linked, on-demand resources available inside the immutable call bundle.
    # They are distinguished from the rules actually loaded into the prompt.
    for source in sorted((ROOT / f'dsh-role-{role}').rglob('*')):
        if not source.is_file() or '__pycache__' in source.parts:
            continue
        if not source.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('技能包含越界支持文件')
        relative = str(source.relative_to(ROOT))
        if relative in files:
            continue
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        resources.append({'path': relative, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    (snapshot / 'instructions.md').write_text(prompt, encoding='utf-8')
    if schema is not None:
        (snapshot / 'output.schema.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2))
    version = re.search(r'^\s+version:\s*["\']?([\w.-]+)', entry.split('---', 2)[1], re.M)
    manifest = {'role': role, 'action': action, 'version': version[1] if version else 'unversioned', 'files': entries, 'resources': resources,
                'binding_hash': binding_hash,
                'selection': {k: selection.get(k) for k in ('provider', 'model', 'reasoning_effort')},
                'instruction_sha256': instruction_hash, 'schema_sha256': schema_hash}
    if workflow:
        manifest['workflow'] = {key: workflow[key] for key in ('version', 'role', 'action', 'workflow_hash', 'steps')}
    (snapshot / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return prompt + '\n技能支持文件快照目录：' + str(snapshot / f'dsh-role-{role}'), manifest


def compose(action, request, material, spec=None):
    """按阶段及测试模式选择业务规则，与底层执行器无关。"""
    product, record = request["product"], request["record"]
    cooperating = not spec and action in ("plan", "develop", "validate") and product.get("intelligence", {}).get("collaboration_enabled")
    skill_rule = rule
    instruction=(
        skill_rule('codex_executor-discovery-1')
        if action in ('discover','daily_attribution') else
        skill_rule('codex_executor-verification-1')
    )
    if action=='investigate':
        instruction=skill_rule('codex_executor-discovery-2')
    if action=='daily_attribution':
        instruction=instruction.replace('最多返回 3 个互不重复的需求。','逐一分析全部输入信号，合并同一问题，不限制本批需求数量。')
        instruction+=skill_rule('codex_executor-discovery-3')
    if action=='daily_retrospective':
        instruction=skill_rule('codex_executor-retrospective-1')
    elif action=='daily_acceptance':
        instruction+=skill_rule('codex_executor-verification-3')
    from .project import generic
    if generic(product):
        instruction=instruction.replace('仅同源 /ths-octop*/api/ 下的只读 GET 路径', '仅项目配置 readonly_paths 允许的同源只读 GET 路径').replace('只能提供同源 /ths-octop*/api/ 路径', '只能提供项目配置 readonly_paths 允许的同源路径').replace('休市', '外部服务不可用')
        instruction += skill_rule('codex_executor-verification-2')
        material['project_config']=product.get('project_config', {})
    if action in ('plan','develop'):
        material.update(plan=record.get('plan'), feedback=record.get('feedback'))
        instruction=rule('plan') if action=='plan' else rule('develop')
    if cooperating:
        from .collaboration import INSTRUCTION
        instruction += INSTRUCTION
        material['collaboration_context'] = request.get('collaboration_context',[])
    if spec:
        material = spec['material']
        instruction = spec['instruction']
        if spec.get('images'):
            material['image_paths'] = spec['images']
            instruction += ' 逐一读取 image_paths 的图片，不能用文件名推断图片内容。'
    if product.get('test_execution') == 'local':
        if action == 'validate':
            instruction = skill_rule('codex_executor-verification-4')
        elif action == 'develop':
            instruction = skill_rule('codex_executor-implementation-1')
        else:
            instruction += rule('local-testing')
    if not spec and action in ('plan', 'develop', 'validate'):
        from .acceptance_scope import PRE_RELEASE_INSTRUCTION, pre_release
        instruction += PRE_RELEASE_INSTRUCTION
        material['acceptance_scope'] = pre_release(request.get('requirement'), record, request.get('test_instance'))
    verification = material.get('verification')
    if action == 'validate' and verification:
        instruction += rule('controller-diff').format(source=verification.get('source', 'worktree'),
            merge_base=verification.get('merge_base'), changed_count=len(verification.get('changed_files') or []),
            diff_sha256=verification.get('diff_sha256') or '')
    return instruction, material
