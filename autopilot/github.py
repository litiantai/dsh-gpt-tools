"""GitHub 仓库配置、凭据引用和可重放的 PR 操作。"""
import json
import base64
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def repository(url):
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?', url or '')
    if not match:
        raise ValueError('Git 地址必须是 GitHub HTTPS 或 SSH 仓库地址，不得包含凭据')
    if any(part in ('.', '..') for part in match[1].split('/')):
        raise ValueError('GitHub 仓库名称无效')
    return match[1]


def load_pull_request(record):
    """通过台账中的 PR 地址读取远端，拒绝跨仓库或分支错配。"""
    match = re.fullmatch(r'https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?', record.get('pr_url') or '')
    repo = repository(record['git_url'])
    if not match or match[1].lower() != repo.lower():
        raise ValueError('缺少有效的已保存 PR 地址，或 PR 不属于当前仓库')
    number = int(match[2])
    if record.get('pr_number') not in (None, number):
        raise ValueError('已保存的 PR 地址与编号不一致')
    gh = GitHub(record['git_url'])
    pr = gh.api('/pulls/' + str(number))
    if pr.get('number') != number:
        raise ValueError('GitHub 返回的 PR 编号与已保存地址不一致')
    for side, branch in [('head', record['branch']), ('base', record['base_branch'])]:
        value = pr.get(side, {})
        if value.get('ref') != branch or (value.get('repo') or {}).get('full_name', '').lower() != repo.lower():
            raise ValueError('PR 仓库或分支已变化，不能继续当前交付评审')
        if not re.fullmatch(r'[0-9a-f]{40,64}', value.get('sha', '')):
            raise ValueError('PR 缺少有效的提交版本')
    return gh, pr


def credential_file():
    """本机运行时共享凭据文件，与项目源码和业务台账隔离。"""
    return Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh'))) / 'supervisor/credentials/github-token'


def save_credential(url, token):
    """验证后原子替换本机 Token；失败保留已有凭据。"""
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_]{20,255}', token):
        raise ValueError('Token 格式无效，请输入完整的 GitHub Personal Access Token')
    result = GitHub(url, token=token).access(write=True)
    path = credential_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix='.github-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result | {'saved': True}


def credential():
    """只在内存中读取系统 Git/GitHub 凭据，不持久化到台账。"""
    path = credential_file()
    if path.exists():
        return path.read_text().strip() or None
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    try:
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    result = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                            capture_output=True, text=True, timeout=15,
                            env=os.environ | {'GIT_TERMINAL_PROMPT': '0'})
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return values.get('password') if result.returncode == 0 else None


def connection_status(url):
    """检查服务进程现有认证；只返回诊断，不保存或回显凭据。"""
    result = {'status': 'blocked', 'checked_at': time.time()}
    if not url:
        return result | {'status': 'unconfigured', 'code': 'unconfigured', 'reason': '尚未配置 GitHub 仓库'}
    try:
        github = GitHub(url)
        result['repository'] = github.repo
        if not credential():
            return result | {'code': 'missing_credentials', 'reason': '本地运行时未找到 GitHub 凭据，推送、创建 PR 和合并已阻塞。服务会复用运行时环境变量、GitHub CLI 或 Git 凭据助手中的认证。'}
        data = github.api()
        if not data.get('permissions', {}).get('push'):
            return result | {'code': 'insufficient_permissions', 'reason': '本地运行时凭据未获 GitHub 仓库推送权限，推送、创建 PR 和合并已阻塞。'}
        if data.get('allow_merge_commit') is False:
            return result | {'code': 'merge_disabled', 'reason': '仓库未允许 merge commit，自动合并已阻塞。'}
        github.probe_push()
        return result | {'status': 'pass', 'code': 'ready', 'reason': '本地运行时认证可用，GitHub 已确认仓库推送权限；PR 和合并仍须通过仓库权限与保护规则检查。'}
    except ValueError as exc:
        # Only validation/API errors produced here are safe to show.
        return result | {'code': 'access_denied', 'reason': str(exc)}
    except (OSError, subprocess.SubprocessError):
        return result | {'code': 'unavailable', 'reason': '无法检查本地运行时 GitHub 认证或连接仓库，推送与合并暂不可用，请重新检查。'}


class GitHub:
    def __init__(self, url, token=None):
        self.repo = repository(url)
        self.token = token

    def api(self, path='', method='GET', body=None):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'dsh-delivery'}
        token = self.token or credential()
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if method != 'GET' and not token:
            raise ValueError('本地运行时未找到 GitHub 凭据，推送、创建 PR 和合并已阻塞')
        request = urllib.request.Request('https://api.github.com/repos/' + self.repo + path,
                                        data=json.dumps(body).encode() if body is not None else None,
                                        headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not reflect request headers or authentication responses into evidence.
            raise ValueError(f'GitHub {method} {path or self.repo} 返回 {exc.code}，请检查权限、分支及保护规则') from None

    def access(self, write=False):
        if write and not (self.token or credential()):
            raise ValueError('本地运行时未找到 GitHub 凭据，推送、创建 PR 和合并已阻塞')
        data = self.api()
        if write and not data.get('permissions', {}).get('push'):
            raise ValueError('GitHub 未确认当前身份具有仓库推送权限')
        if write and data.get('allow_merge_commit') is False:
            raise ValueError('仓库未允许 merge commit，请先启用此合并方式')
        if write:
            self.probe_push()
        return {'repository': self.repo, 'accessible': True, 'can_push': data.get('permissions', {}).get('push', False)}

    def probe_push(self):
        """只读取 Git 接收服务的引用公告，检查 Token 权限而不修改远端。"""
        token = self.token or credential()
        if not token:
            raise ValueError('本地运行时未找到 GitHub 凭据')
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        request = urllib.request.Request('https://github.com/' + self.repo + '.git/info/refs?service=git-receive-pack',
            headers={'Authorization': 'Basic ' + auth, 'User-Agent': 'dsh-delivery'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.headers.get('Content-Type', '').split(';')[0] != 'application/x-git-receive-pack-advertisement':
                    raise ValueError('GitHub 未返回可用的 Git 推送服务，不能确认 Token 写入权限')
        except urllib.error.HTTPError as exc:
            raise ValueError(f'GitHub 推送认证返回 HTTP {exc.code}；请检查 Token 是否授权当前仓库及 Contents 写入权限') from None

    def pull_request(self, branch, base, title, body, successive=False):
        from urllib.parse import urlencode
        query = urlencode({'head': self.repo.split('/')[0] + ':' + branch, 'base': base, 'state': 'open' if successive else 'all', 'per_page': 100})
        matches = self.api('/pulls?' + query)
        if matches:
            pr = matches[0]
            if pr['state'] != 'open':
                raise ValueError('该日期的 PR 已关闭或合并，禁止向已收口批次追加成果')
            return self.api('/pulls/' + str(pr['number']), 'PATCH', {'title': title, 'body': body})
        return self.api('/pulls', 'POST', {'head': branch, 'base': base, 'title': title, 'body': body})

    def review_report(self, number, round_id, result):
        """每轮发布独立评审报告；重放沿用已有评论，保留失败历史。"""
        from urllib.parse import quote
        marker = '<!-- dsh-code-review:' + str(round_id) + ' -->'
        route = '/issues/' + str(number) + '/comments'
        page = 1
        while True:
            comments = self.api(route + f'?per_page=100&page={page}')
            for comment in comments:
                if comment.get('body', '').startswith(marker + '\n'):
                    return comment
            if len(comments) < 100:
                break
            page += 1
        title = {'pass': '代码评审通过', 'fail': '代码评审未通过'}.get(result['status'],
            '评审 Agent 运行异常' if result.get('failure_kind') == 'agent_execution' else '代码评审阻塞')
        lines = [marker, '## ' + title,
            '', '评审提交：`' + result['head_sha'] + '`', '目标基线：`' + result['base_sha'] + '`',
            '', '### 结论', result.get('reason') or result.get('summary') or '请查看下方问题列表。']
        if result.get('summary') and result.get('summary') != result.get('reason'):
            lines += ['', result['summary']]
        lines += ['', '### 问题明细']
        issues = result.get('issues', [])
        if not issues:
            lines.append('本轮未形成有效代码评审结论。' if result['status'] == 'blocked' else '未报告具体代码问题。')
        for index, issue in enumerate(issues, 1):
            path = str(issue.get('path', ''))
            line = issue.get('start_line', 1)
            link = f'https://github.com/{self.repo}/blob/{result["head_sha"]}/{quote(path, safe="/")}#L{line}'
            lines += ['', f'{index}. **{issue.get("severity", "unknown")}** · [{path}:{line}]({link})',
                '', str(issue.get('content', '未提供问题说明'))]
        lines += ['', f'文件覆盖：{result.get("reviewed_files", 0)} / {result.get("total_files", 0)}',
            '评审轮次：`' + str(round_id) + '`', '', '此报告绑定上述提交；后续修复与复审会保留本轮记录。']
        body = '\n'.join(lines)
        if len(body) > 60000:
            body = body[:59000] + '\n\n报告内容超出 GitHub 评论长度，完整结果保留在监工看板本轮评审记录中。'
        return self.api(route, 'POST', {'body': body})

    def status(self, sha, state, description, target_url=None):
        body = {'state': state, 'context': 'dsh/code-delivery', 'description': description[:140]}
        if target_url:
            body['target_url'] = target_url
        return self.api('/statuses/' + sha, 'POST', body)

    def checks_pass(self, sha):
        # Enumerate every page; latest check per app/name wins after a rerun.
        checks = {}
        page = 1
        while True:
            rows = self.api(f'/commits/{sha}/check-runs?per_page=100&page={page}').get('check_runs', [])
            for row in rows:
                key = (row.get('app', {}).get('id'), row['name'])
                if key not in checks or row['id'] > checks[key]['id']:
                    checks[key] = row
            if len(rows) < 100:
                break
            page += 1
        if any(r['status'] != 'completed' or r.get('conclusion') not in ('success', 'neutral', 'skipped') for r in checks.values()):
            return False
        states = self.api(f'/commits/{sha}/status?per_page=100')
        return states['state'] == 'success'

    def merge(self, number, sha):
        pr = self.api('/pulls/' + str(number))
        if pr.get('merged'):
            return pr
        if pr['head']['sha'] != sha or pr['state'] != 'open':
            raise ValueError('PR 版本已变化或已关闭，必须重新评审')
        self.api('/pulls/' + str(number) + '/merge', 'PUT', {'sha': sha, 'merge_method': 'merge'})
        verified = self.api('/pulls/' + str(number))
        if not verified.get('merged') or not verified.get('merge_commit_sha'):
            raise ValueError('GitHub 尚未确认 PR 合并成功')
        return verified
