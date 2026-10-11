"""角色内部有序工作流、技术栈标准和不可变资源快照。"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import subprocess

PHASES = ('install', 'compile', 'typecheck', 'build', 'test', 'browser')
STACKS = ('generic', 'node', 'python', 'java', 'react', 'vue')


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=128)
def parse(text):
    helper = Path(__file__).resolve().parents[1] / 'scripts/read-workflow.mjs'
    try:
        result = subprocess.run(['node', str(helper)], input=text, text=True, capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError(result.stderr[-1500:])
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('无法解析角色工作流，请安装本机 Node.js 并安装项目依赖：' + str(exc)) from exc
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('不支持的角色工作流版本')
    return value


def load(role, action=None):
    from . import role_skills
    relative = f'dsh-role-{role}/workflow.yaml'
    if not (role_skills.ROOT / relative).exists() and not (role_skills.ROOT / relative).is_symlink():
        return None
    resources = {}
    def read(path):
        text = role_skills.read(path)
        resources[path] = text
        return text
    definition = parse(read(relative))
    if definition.get('role') != role or not isinstance(definition.get('steps'), list) or not definition['steps']:
        raise ValueError('角色工作流缺少有效角色或步骤')
    ids = set()
    for step in definition['steps']:
        if not isinstance(step, dict) or not re.fullmatch(r'[a-z][a-z0-9_-]*', str(step.get('id', ''))) or step['id'] in ids:
            raise ValueError('工作流步骤标识无效或重复')
        ids.add(step['id'])
        if not isinstance(step.get('title'), str) or not step['title'] or step.get('kind') not in ('instruction', 'quality', 'runtime'):
            raise ValueError('工作流步骤类型或说明无效')
        for field in ('actions', 'stacks', 'rules'):
            if field in step and (not isinstance(step[field], list) or not all(isinstance(s, str) and s for s in step[field])):
                raise ValueError('工作流条件与规则必须为字符串数组')
        if 'stacks' in step and not set(step['stacks']) <= set(STACKS):
            raise ValueError('工作流包含未知技术栈')
        for rule in step.get('rules', []):
            read(rule)
    profiles = {}
    refs = definition.get('profiles', {})
    if not isinstance(refs, dict) or not set(refs) <= set(STACKS):
        raise ValueError('工作流技术栈索引无效')
    for stack, path in refs.items():
        profile = parse(read(path))
        phases = profile.get('phases')
        if profile.get('stack') != stack or not isinstance(phases, list):
            raise ValueError('语言验收标准无效')
        seen = []
        for phase in phases:
            if (not isinstance(phase, dict) or phase.get('command') not in PHASES
                    or phase['command'] in seen or phase.get('required') not in ('always', 'configured')):
                raise ValueError('语言检查步骤无效')
            seen.append(phase['command'])
        if seen != sorted(seen, key=PHASES.index):
            raise ValueError('语言检查顺序必须先安装、编译，再构建和测试')
        if stack == 'java' and not any(p['command'] == 'compile' and p['required'] == 'always' for p in phases):
            raise ValueError('Java 验收必须包含编译门禁')
        for rule in profile.get('rules', []):
            read(rule)
        profiles[stack] = profile
    steps = [s for s in definition['steps'] if not action or not s.get('actions') or action in s['actions']]
    return {'version': 1, 'role': role, 'action': action, 'steps': steps, 'profiles': profiles,
            'resources': resources, 'workflow_hash': sha(resources)}


def detect_stack(root):
    root = Path(root)
    if any((root / p).is_file() for p in ('pom.xml', 'build.gradle', 'build.gradle.kts')):
        return 'java'
    if (root / 'package.json').is_file():
        data = json.loads((root / 'package.json').read_text())
        deps = dict(data.get('dependencies', {}), **data.get('devDependencies', {}))
        return 'vue' if 'vue' in deps else 'react' if 'react' in deps else 'node'
    return 'python' if any((root / p).is_file() for p in ('pyproject.toml', 'requirements.txt')) else 'generic'


def modules(workspace, config):
    root = Path(workspace).resolve()
    items = config.get('modules', [])
    result = []
    if config.get('commands') or not items:
        result.append({'id': 'root', 'path': '.', 'stack': config.get('stack') or ('generic' if items else detect_stack(root)),
                       'commands': config.get('commands', {}), 'install_policy': config.get('install_policy')})
    result.extend(items)
    resolved = []
    ids = set()
    for item in result:
        ident, path = item.get('id'), item.get('path')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', ident) or ident in ids:
            raise ValueError('模块标识无效或重复')
        ids.add(ident)
        if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('模块目录必须位于项目内')
        directory = (root / path).resolve()
        if not directory.is_relative_to(root) or not directory.is_dir():
            raise ValueError('模块目录不存在或越界：' + path)
        stack = item.get('stack') or detect_stack(directory)
        if stack not in STACKS:
            raise ValueError('未知技术栈，请使用 generic 并声明项目命令')
        resolved.append(item | {'stack': stack, 'workspace': str(directory)})
    return resolved


def instruction(workflow, config=None, workspace=None):
    if workflow is None:
        return ''
    lines = ['角色工作流（按顺序执行；命令检查由控制器回执确认，口头声明不能代替通过证据）：']
    stacks = {m['stack'] for m in modules(workspace, config or {})} if workspace else set(STACKS)
    for step in workflow['steps']:
        if not step.get('stacks') or stacks.intersection(step['stacks']):
            lines.append(f"- {step['id']}：{step['title']}")
            for rule in step.get('rules', []):
                lines.append(workflow['resources'][rule])
    for stack in sorted(stacks):
        profile = workflow['profiles'].get(stack) or workflow['profiles'].get('generic')
        if profile:
            lines.append(stack + ' 检查顺序：' + ' → '.join(p['command'] for p in profile['phases']))
            for rule in profile.get('rules', []):
                lines.append(workflow['resources'][rule])
    return '\n'.join(lines)


def reported(manifest, result):
    """展示模型执行的声明，不把声明转换为可信质量通过。"""
    workflow = manifest.get('workflow')
    if not workflow:
        return None
    return workflow | {'status': result.get('status', 'pending'), 'steps': [
        step | {'status': 'pending' if step['kind'] == 'quality' else
                'reported' if result.get('status') == 'pass' else 'pending'}
        for step in workflow['steps']]}
