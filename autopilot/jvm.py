"""JVM 命令的显式依赖准备、独立缓存与离线质量检查。"""
import os
from pathlib import Path
import shutil
import subprocess


class MissingRuntime(ValueError):
    """本机运行时缺失及可展示的安装提示；不触发自动安装。"""
    def __init__(self, tool, hint):
        self.tool = tool
        self.hint = hint
        super().__init__('本机缺少 ' + tool + '：' + hint)


def kind(argv):
    name = Path(argv[0]).name if argv else ''
    return 'maven' if name in ('mvn', 'mvnw') else 'gradle' if name in ('gradle', 'gradlew') else None


def java_home():
    value = os.environ.get('JAVA_HOME')
    if value and (Path(value) / 'bin/javac').is_file():
        return str(Path(value).resolve())
    if Path('/usr/libexec/java_home').exists():
        probe = subprocess.run(['/usr/libexec/java_home'], capture_output=True, text=True, timeout=10)
        if probe.returncode == 0 and (Path(probe.stdout.strip()) / 'bin/javac').is_file():
            return probe.stdout.strip()
    executable = shutil.which('javac')
    if executable and str(Path(executable).resolve()) != '/usr/bin/javac':
        return str(Path(executable).resolve().parents[1])
    raise MissingRuntime('JDK', '请安装项目要求版本的 JDK，并设置 JAVA_HOME；安装后重试。')


def install_allowed(argv, policy):
    tool = kind(argv)
    if not tool or policy != 'jvm-resolve':
        return False
    # Maven's fixed dependency goal and a project-declared Gradle resolver are
    # explicitly opt-in: these tools execute plugins/build scripts even at resolution time.
    goals = [s for s in argv[1:] if not s.startswith('-')]
    return goals == (['dependency:go-offline'] if tool == 'maven' else ['autopilotResolveDependencies'])


def prepare(argv, root, workspace, *, install=False):
    tool = kind(argv)
    if not tool:
        return list(argv)
    java_home()
    executable = argv[0]
    if Path(executable).name in ('mvnw', 'gradlew'):
        label, command = ('Maven', 'mvn') if tool == 'maven' else ('Gradle', 'gradle')
        raise MissingRuntime(label, f'请安装项目要求版本的 {label}，并将命令配置为本机 {command} 路径；不通过 wrapper 自动下载工具链。')
    exists = (Path(workspace) / executable).is_file() if '/' in executable else shutil.which(executable)
    if not exists:
        label = 'Maven' if tool == 'maven' else 'Gradle'
        raise MissingRuntime(label, f'请安装项目要求版本的 {label}，加入 PATH 或在项目命令中配置绝对路径；安装后重试。')
    root = Path(root).resolve()
    forbidden = ('-Dmaven.repo.local', '-Duser.home', '-Dgradle.user.home', '--gradle-user-home', '-g',
                 '-Porg.gradle.java.installations.auto-download', '-Dorg.gradle.java.installations.auto-download')
    if any(any(s == key or s.startswith(key+'=') for key in forbidden) for s in argv[1:]):
        raise ValueError('JVM 缓存目录由控制器管理，不能在项目命令中覆盖')
    if tool == 'maven':
        options = ['-B', '-Duser.home='+str(root/'home'), '-Dmaven.repo.local='+str(root/'cache/maven')]
        if not install:
            options.append('-o')
    else:
        options = ['--no-daemon', '--gradle-user-home', str(root/'cache/gradle'),
                   '-Porg.gradle.java.installations.auto-download=false']
        if not install:
            options.append('--offline')
    return [executable, *options, *argv[1:]]


def environment(root):
    root = Path(root).resolve()
    home = java_home()
    return {'JAVA_HOME': home, 'PATH': str(Path(home)/'bin')+os.pathsep+os.environ.get('PATH', ''),
            'JAVA_TOOL_OPTIONS': '-Djava.net.preferIPv4Stack=true',
            'GRADLE_USER_HOME': str(root/'cache/gradle'),
            'MAVEN_USER_HOME': str(root/'cache/maven-home')}


def classify(check):
    """工具链和离线依赖缺失属于环境阻塞，编译诊断及测试断言保留失败。"""
    import re
    text = check.get('log_tail', '') + '\n' + check.get('reason', '')
    if check.get('status') == 'fail' and re.search(
            r'Cannot find a Java installation|No matching toolchains found|Toolchain download repositories|'
            r'invalid target release|release version \d+ not supported', text, re.I):
        hint = '请安装项目要求版本的 JDK，并设置 JAVA_HOME；不会自动下载 JDK。'
        return check | {'status': 'blocked', 'failure_kind': 'environment', 'missing_tools': ['兼容版本 JDK'],
                        'install_hint': hint, 'reason': hint + '\n' + text[-2000:]}
    if check.get('status') == 'fail' and re.search(
            r'has not been downloaded|No cached version|Cannot access .+offline|'
            r'Could not resolve .+offline|JAVA_HOME is not defined correctly|'
            r'Unable to locate a Java Runtime|Could not find or load main class|'
            r'Could not transfer artifact|UnknownHostException|Connection timed out|SocketException: Operation not permitted', text, re.I):
        return check | {'status': 'blocked', 'failure_kind': 'environment', 'reason': 'JVM 工具链或依赖环境阻塞：' + text[-2000:]}
    return check
