"""项目接入时识别技术栈，只探测本机工具版本，不安装或运行项目脚本。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from . import jvm

MANIFESTS = ('pom.xml', 'build.gradle', 'build.gradle.kts', 'package.json', 'pyproject.toml', 'requirements.txt')
SKIP = {'.git', '.agent', '.codex', '.venv', 'venv', 'node_modules', 'target', 'build', 'dist', '.gradle', '__pycache__'}
HINTS = {
    'java': '请安装项目要求版本的 JDK，并设置 JAVA_HOME；安装后重新检测。',
    'javac': '请安装完整 JDK（包含 javac），并设置 JAVA_HOME。',
    'mvn': '请安装项目要求版本的 Maven，将 mvn 加入管理服务的 PATH，或配置本机绝对路径。',
    'gradle': '请安装项目要求版本的 Gradle，将 gradle 加入管理服务的 PATH，或配置本机绝对路径。',
    'node': '请安装项目要求版本的 Node.js，并加入管理服务的 PATH。',
    'npm': '请安装 Node.js 自带的 npm，并加入管理服务的 PATH。',
    'pnpm': '请在本机安装项目要求版本的 pnpm，并加入管理服务的 PATH。',
    'yarn': '请在本机安装项目要求版本的 Yarn，并加入管理服务的 PATH。',
    'python3': '请安装项目要求版本的 Python 3，并加入管理服务的 PATH。',
}


def describe(workspace, config=None):
    """识别各目录清单；显式模块配置覆盖推断，不擅自生成 Java 构建命令。"""
    config = config or {}
    root = Path(workspace).resolve()
    modules, warnings = [], []
    declared = {str(Path(m['path'])): m for m in config.get('modules', [])}
    declared.setdefault('.', config)
    directories = {}
    if root.is_dir():
        for index, (directory, dirs, files) in enumerate(os.walk(root, followlinks=False)):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.') and not (Path(directory)/d).is_symlink())
            if index >= 2000:
                warnings.append('目录数量超过识别上限，请通过 modules 明确其余模块。')
                break
            if len(Path(directory).relative_to(root).parts) >= 8:
                dirs[:] = []
            evidence = [name for name in MANIFESTS if name in files and not (Path(directory)/name).is_symlink()]
            if evidence:
                directories[str(Path(directory).relative_to(root))] = evidence
    for path in declared:
        directories.setdefault(path, [])
    for path, files in sorted(directories.items()):
        folder = (root/path).resolve()
        if not folder.is_relative_to(root):
            raise ValueError('模块目录不能越过项目目录')
        setting = declared.get(path, {})
        stacks, tools = [], []
        if any(name in files for name in ('pom.xml', 'build.gradle', 'build.gradle.kts')):
            stacks.append('java'); tools.extend(['java', 'javac'])
            tools.extend(['mvn'] if 'pom.xml' in files else ['gradle'])
        manager = 'pnpm' if (folder/'pnpm-lock.yaml').exists() else 'yarn' if (folder/'yarn.lock').exists() else 'npm'
        if 'package.json' in files:
            try:
                package = json.loads((folder/'package.json').read_text())
                deps = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
                stacks.extend([s for s in ('react', 'vue') if s in deps] or ['node'])
                requested = package.get('packageManager', '').split('@')[0]
                if requested in ('npm', 'pnpm', 'yarn'):
                    manager = requested
            except (ValueError, TypeError, AttributeError):
                warnings.append(f'{path}/package.json 无法解析')
                stacks.append('node')
            tools.extend(['node', manager])
        if any(name in files for name in ('pyproject.toml', 'requirements.txt')):
            stacks.append('python'); tools.append('python3')
        if setting.get('stack'):
            stacks = stacks or [setting['stack']]
            # 显式选择决定工作流，清单中已发现的工具仍需展示。
            if setting['stack'] == 'java': tools.extend(['java', 'javac'])
            if setting['stack'] in ('react', 'vue', 'node'): tools.extend(['node', manager])
            if setting['stack'] == 'python': tools.append('python3')
        executables = {}
        for commands in setting.get('commands', {}).values():
            for argv in commands:
                if not argv: continue
                name = Path(argv[0]).name
                tool = {'mvnw': 'mvn', 'gradlew': 'gradle'}.get(name, name)
                if tool in HINTS:
                    tools.append(tool)
                    if name in ('mvnw', 'gradlew'):
                        warnings.append(f'{path} 配置了 {name}，请改用已安装的本机 {tool}；不会自动下载 wrapper 工具链。')
                    elif '/' in argv[0]:
                        executables[tool] = str((folder/argv[0]).resolve())
        if stacks or tools or setting.get('stack') or path != '.':
            modules.append({'path': path, 'stacks': stacks or ['generic'], 'workflow_stack': setting.get('stack'), 'selection': 'configured' if setting.get('stack') else 'detected',
                            'evidence': [str(Path(path)/name) for name in files], 'tools': sorted(set(tools)), 'executables': executables})
    # 聚合构建的子模块沿用最近父模块明确配置的本机工具路径。
    for module in modules:
        ancestors = sorted((m for m in modules if m is not module
                            and (root/module['path']).is_relative_to(root/m['path'])),
                           key=lambda m: len(Path(m['path']).parts), reverse=True)
        for parent in ancestors:
            for tool, executable in parent['executables'].items():
                if tool in module['tools']:
                    module['executables'].setdefault(tool, executable)
    return {'modules': modules, 'stacks': sorted({s for m in modules for s in m['stacks']}), 'warnings': warnings}


def check(workspace, config=None):
    description = describe(workspace, config)
    requirements = {}
    for module in description['modules']:
        for tool in module['tools']:
            executable = module['executables'].get(tool)
            requirements.setdefault((tool, executable), []).append(module['path'])
    checks = []
    for (tool, configured), modules in requirements.items():
        entry = {'tool': tool, 'modules': modules, 'status': 'missing', 'install_hint': HINTS[tool]}
        try:
            home = jvm.java_home() if tool in ('java', 'javac', 'mvn', 'gradle') else None
            executable = configured or (str(Path(home)/'bin'/tool) if tool in ('java', 'javac') else shutil.which(tool))
            if not executable or not Path(executable).is_file():
                checks.append(entry); continue
            executable = str(Path(executable).absolute())
            entry['path'] = executable
            # 不把仓库里的同名脚本当成本机已安装工具运行。
            if Path(executable).resolve().is_relative_to(Path(workspace).resolve()):
                raise ValueError('工具路径位于项目内部，请配置本机安装的工具路径。')
            with tempfile.TemporaryDirectory(prefix='dsh-runtime-check-') as directory:
                env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LANG', 'TMPDIR') if k in os.environ}
                env.update(HOME=directory, COREPACK_ENABLE_NETWORK='0', COREPACK_ENABLE_PROJECT_SPEC='0',
                           npm_config_update_notifier='false', GRADLE_USER_HOME=directory, MAVEN_SKIP_RC='true')
                if home:
                    env.update(JAVA_HOME=home, PATH=str(Path(home)/'bin')+os.pathsep+env.get('PATH', ''))
                argv = [executable, '-version' if tool in ('java', 'javac') else '--version']
                entry['command'] = argv
                result = subprocess.run(argv, cwd=directory, env=env, capture_output=True, text=True, timeout=12)
                output = (result.stdout+'\n'+result.stderr).strip()[-4000:]
                entry.update(exit_code=result.returncode, version=output, status='installed' if result.returncode == 0 and output else 'error')
                if entry['status'] == 'error': entry['reason'] = '工具版本命令未成功返回，请检查安装与环境变量。'
        except jvm.MissingRuntime as error:
            entry.update(reason=str(error), install_hint=error.hint)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            entry.update(status='error', reason=str(error))
        checks.append(entry)
    return description | {'checked_at': time.time(), 'checks': checks,
                          'status': 'ready' if checks and all(c['status'] == 'installed' for c in checks) else 'blocked' if checks else 'unknown'}


def context(ledger, kind, ident):
    record = ledger.get(kind, ident)
    if kind == 'products':
        workspace = record.get('source') or record.get('repository')
        if not workspace or not Path(workspace).is_dir():
            raise ValueError('项目源码目录尚未就绪，请先获取仓库。')
        return Path(workspace), record.get('project_config', {})
    root = ledger.store.state/'autopilot/scans'/ident
    path = root/'environment.json'
    if path.is_file():
        saved = json.loads(path.read_text())
        return root/'workspace', saved['configuration']
    result = record.get('result', {})
    if result.get('configuration') is not None and (root/'workspace').is_dir():
        return root/'workspace', result['configuration']
    raise ValueError('尚未获取仓库，完成源码获取后即可检测本机运行时。')
