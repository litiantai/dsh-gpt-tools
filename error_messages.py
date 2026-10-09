"""控制台共用异常目录：中文说明与脱敏诊断分离，不依赖模型翻译。"""
import errno
import json
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError, URLError

CATALOG = json.loads((Path(__file__).parent/'dashboard/src/error-catalog.json').read_text())
TECHNICAL = re.compile(r'Traceback|\b(?:\w*Error|Exception)\b|Errno|https?://|/(?:Users|home|tmp|var|private)/|\b(?:QUOTA|ECONN\w*|ENOENT)\b|\b(?:pnpm|npm|node|python)\s|\{["\']', re.I)
SECRET_KEYS = re.compile(r'token|password|cookie|authorization|api.?key|secret|credential', re.I)


def sanitize(value):
    if isinstance(value, dict):
        return {k: '[已隐藏]' if SECRET_KEYS.search(k) else sanitize(v) for k,v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(Bearer\s+)[\w.\-/+=]+', r'\1[已隐藏]', value)
        value = re.sub(r'(?i)((?:["\']?(?:api[_-]?key|token|password|cookie|authorization|secret|credential)["\']?)\s*[=:]\s*)(?:"[^"\n]*"|\x27[^\x27\n]*\x27|[^\s,;}]+)', r'\1[已隐藏]', value)
        value = re.sub(r'(https?://[^\s?]+)\?[^\s]*', r'\1?[已隐藏]', value)
        value = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[已隐藏]@', value)
        return value[:20000]
    return value


def error_info(error, subject='相关服务', code=None):
    """优先使用异常类型与错误号，其次兼容历史错误字符串。"""
    raw = str(error)
    cause = error.reason if isinstance(error, URLError) and isinstance(error.reason, BaseException) else error
    if code is None:
        if isinstance(error, HTTPError):
            code = {401:'AUTH_REQUIRED',403:'ACCESS_DENIED',402:'MODEL_BALANCE_INSUFFICIENT',429:'RATE_LIMITED',502:'SERVICE_UNAVAILABLE',503:'SERVICE_UNAVAILABLE',504:'REQUEST_TIMEOUT'}.get(error.code)
        elif isinstance(cause, TimeoutError): code = 'REQUEST_TIMEOUT'
        elif isinstance(cause, FileNotFoundError): code = 'FILE_MISSING'
        elif isinstance(cause, PermissionError): code = 'ACCESS_DENIED'
        elif isinstance(cause, json.JSONDecodeError): code = 'INVALID_RECEIPT'
        elif isinstance(cause, subprocess.CalledProcessError): code = 'PROCESS_FAILED'
        elif isinstance(cause, OSError):
            code = {errno.ECONNREFUSED:'CONNECTION_REFUSED',errno.ECONNRESET:'CONNECTION_LOST',errno.EPIPE:'CONNECTION_LOST'}.get(cause.errno)
    row = next((r for r in CATALOG if r['code']==code), None) if code else None
    row = row or next((r for r in CATALOG if re.search(r['pattern'],raw,re.I)), None)
    if row is None and re.search(r'[\u4e00-\u9fff]',raw) and not TECHNICAL.search(raw):
        return {'code':'BUSINESS','message':raw.strip('"\''),'suggestion':''}
    row = row or CATALOG[-1]
    return {k: row[k].replace('{subject}',subject) for k in ('code','message','suggestion')}


def failure_fields(error, subject='相关服务', code=None):
    info = error_info(error,subject,code)
    return {'reason':info['message']+info['suggestion'],'error_info':info,'diagnostic':sanitize(str(error))}


def present_errors(value):
    """为新回执和接口数据添加中文展示信息，原始错误保留在诊断字段。"""
    if isinstance(value,list): return [present_errors(v) for v in value]
    if not isinstance(value,dict): return value
    result = {k: v if k in ('diagnostic','error_info') else present_errors(v) for k,v in value.items()}
    if isinstance(value.get('error_info'),dict):
        if 'diagnostic' in result: result['diagnostic'] = sanitize(result['diagnostic'])
        return result
    fields = ['reason','error','reporting_error']
    if value.get('status') in ('fail','failed','blocked','unknown','busy'):
        fields += ['summary','actual']
    originals = {}
    for key in fields:
        raw = value.get(key)
        if not isinstance(raw,str) or not raw: continue
        info = error_info(raw)
        if info['code']=='BUSINESS': continue
        originals[key] = raw
        result[key] = info['message']+info['suggestion']
        if 'error_info' not in result: result['error_info'] = info
    if originals:
        result['diagnostic'] = sanitize({'original':originals,'detail':value.get('diagnostic')})
    return result
