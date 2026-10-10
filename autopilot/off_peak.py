"""DeepSeek 官方优惠时段准入；排队保存在台账，不启动等待进程。"""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
# 官方规则核对于 2026/10/09：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
# 国办发明电〔2025〕7号（完整放假日期；周末调休上班仍按周末放行）：
# https://www.gov.cn/zhengce/content/202511/content_7047090.htm
HOLIDAYS = {2026: (('01-01', '01-03'), ('02-15', '02-23'), ('04-04', '04-06'),
                   ('05-01', '05-05'), ('06-19', '06-21'), ('09-25', '09-27'),
                   ('10-01', '10-07'))}


def window(now=None):
    """返回当前时段及下一个放行时间；未收录年份保守按普通工作日处理。"""
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, ZONE)
    holiday = any(start <= local.strftime('%m-%d') <= end for start, end in HOLIDAYS.get(local.year, ()))
    end_hour = None
    if local.weekday() < 5 and not holiday:
        for start, end in ((9, 12), (14, 18)):
            if start <= local.hour < end:
                end_hour = end
                break
    resume = local.replace(hour=end_hour, minute=0, second=0, microsecond=0).timestamp() if end_hour else None
    return {'peak': end_hour is not None, 'next_allowed_at': resume, 'timezone': 'Asia/Shanghai',
            'holiday_calendar_year': local.year, 'holiday_calendar_known': local.year in HOLIDAYS}


def deepseek(selected):
    """识别官方供应商；旧 Harness 继承模型时保守应用时段限制。"""
    selected = selected or {}
    provider = selected.get('model_provider', '')
    if provider == 'deepseek-official':
        return True
    if selected.get('provider') != 'harness':
        return False
    return not provider or provider == 'deepseek'


def selections(product, action):
    """按执行阶段解析角色，非模型阶段不受时段开关限制。"""
    agents = product.get('agents', {})
    if action == 'coordinate':
        from .coordinator import selected
        return [selected(product)]
    if action in ('plan', 'develop'):
        return [agents.get('implementation', {'provider': 'harness'})]
    if action in ('discover', 'investigate', 'daily_attribution'):
        return [agents.get('discovery')]
    if action in ('verify', 'validate', 'validate_release', 'daily_retrospective'):
        return [agents.get('verification')]
    if action == 'daily_acceptance' and not product.get('git', {}).get('enabled'):
        return [agents.get('verification')]
    if action in ('review', 'review_feature', 'review_release', 'repair', 'repair_feature', 'repair_release'):
        choices = product.get('code_review', {})
        repair = action.startswith('repair')
        selected = choices.get('fixer' if repair else 'reviewer') or agents.get('implementation' if repair else 'acceptance')
        return [selected]
    return []


def waiting(product, action, selected=None, now=None):
    """高峰期返回可持久化的等待原因；关闭开关或非 DeepSeek 调用直接放行。"""
    if not product.get('policy', {}).get('deepseek_off_peak_only', True):
        return None
    choices = [selected] if selected is not None else selections(product, action)
    if not any(deepseek(choice) for choice in choices):
        return None
    state = window(now)
    if not state['peak']:
        return None
    resume = datetime.fromtimestamp(state['next_allowed_at'], ZONE).strftime('%Y/%m/%d %H:%M')
    return {'action': action, 'next_allowed_at': state['next_allowed_at'],
            'reason': f'DeepSeek 高峰时段，排队等待北京时间 {resume} 后自动继续'}
