"""邮件样式的事实保留、正文转义和时间窗口语义回归。"""
import copy
import datetime as dt
from html.parser import HTMLParser
import sys
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autopilot.notification_email import render_html

ZONE = ZoneInfo('Asia/Shanghai')


class BodyText(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.text = []
        self.tags = []
        self.attrs = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


class NotificationEmailTests(unittest.TestCase):
    def setUp(self):
        start = dt.datetime(2026, 10, 13, tzinfo=ZONE).timestamp()
        self.data = {
            'product': '样式测试项目', 'kind': 'morning_retro', 'day': '2026-10-13',
            'window': [start, start + 86400], 'focus': '真实事件摘要',
            'stats': {'requirements_found': 12, 'runs_started': 3, 'accepted': 1,
                      'online': 1, 'releases': 0, 'queued': 1},
            'done': [{'id': 'run-id', 'title': '已验收需求'}],
            'online_detail': [{'id': 'delivery-id', 'title': 'release-20261012'}],
            'issues': [{'source': 'task', 'id': 'issue-id', 'title': '待处理问题',
                        'cause': '缺少依赖', 'action': '补齐运行依赖'}],
            'open_problems': [{'summary': '待复盘问题', 'occurrences': 2}],
            'quota': {'tokens_used': 42921456, 'tokens_limit': 120000000,
                      'tokens_exhausted': False, 'development_used': 5, 'development_limit': 5,
                      'discovery_used': 0, 'discovery_limit': 0},
            'competitors': {'jobs': [{'action': 'find_competitors', 'status': 'completed',
                                     'found': 4, 'reason': '核实官方来源'}],
                            'new': [{'name': '竞品名称'}]},
            'todo': ['处理当前问题'], 'suggestions': ['完成后复验'],
        }

    def test_email_keeps_all_report_facts_and_never_mutates_data(self):
        before = copy.deepcopy(self.data)
        source = render_html('昨日简报', self.data)
        body = ''.join(BodyText(source).text)
        for fact in ('真实事件摘要', '已验收需求', 'run-id', 'release-20261012', 'delivery-id',
                     '待处理问题', '缺少依赖', '补齐运行依赖', '待复盘问题', '累计 2 次',
                     '竞品名称', '核实官方来源', '新增 4 个候选', '处理当前问题', '完成后复验'):
            self.assertIn(fact, body)
        self.assertEqual(self.data, before)

    def test_dynamic_content_is_escaped_and_unlisted_fields_are_not_rendered(self):
        hostile = '<img src=x onerror="secret-canary"> & "quote"'
        self.data['product'] = hostile
        self.data['done'][0]['title'] = hostile
        self.data['issues'][0]['cause'] = hostile
        self.data['competitors']['new'][0]['name'] = hostile
        self.data['todo'] = [hostile]
        self.data['credential'] = 'unlisted-credential-canary'
        self.data['sender'] = 'unlisted-sender-canary@example.test'
        source = render_html(hostile, self.data)
        parsed = BodyText(source)
        self.assertNotIn('img', parsed.tags)
        self.assertFalse(any(key.startswith('on') for key, _ in parsed.attrs))
        self.assertIn(hostile, ''.join(parsed.text))
        self.assertNotIn('unlisted-credential-canary', source)
        self.assertNotIn('unlisted-sender-canary', source)

    def test_quota_formats_exact_counts_and_caps_only_the_visual_bar(self):
        body = ''.join(BodyText(render_html('额度报告', self.data)).text)
        self.assertIn('42,921,456', body)
        self.assertIn('120,000,000', body)
        self.assertIn('35.8%', body)
        self.assertIn('不限', body)
        self.data['quota'].update(tokens_used=150, tokens_limit=100, tokens_exhausted=True)
        source = render_html('超额报告', self.data)
        self.assertIn('150.0%', source)
        self.assertIn('width="100.00%"', source)
        self.assertNotIn('width="150.00%"', source)

    def test_daily_and_run_reports_describe_their_actual_time_windows(self):
        daily = render_html('日报', self.data)
        self.assertIn('二、昨日完成', daily)
        self.assertIn('2026-10-13 00:00–24:00', daily)
        self.data['kind'] = 'run_report'
        start = dt.datetime(2026, 10, 13, 10, 15, tzinfo=ZONE).timestamp()
        self.data['window'] = [start, start + 3600]
        report = render_html('运行报告', self.data)
        self.assertIn('二、本次统计窗口完成', report)
        self.assertNotIn('二、昨日完成', report)
        self.assertIn('2026-10-13 10:15 至 2026-10-13 11:15', report)
        self.assertNotIn('00:00–24:00', report)
        self.data['window'] = [start + 1, start]
        with self.assertRaises(ValueError):
            render_html('无效窗口', self.data)

    def test_recovered_and_incomplete_history_are_distinct_from_open_issues(self):
        end = self.data['window'][1]
        self.data['resolved_problems'] = [
            {'summary': '窗口内恢复问题', 'closed_at': end - 1, 'status': 'resolved'},
            {'summary': '复盘前恢复问题', 'closed_at': end + 1, 'status': 'resolved', 'history_incomplete': True},
            {'summary': '恢复时点缺失问题', 'status': 'resolved'},
        ]
        self.data['history_problems'] = [{'summary': '不完整历史问题', 'observed_at': end - 60,
                                          'observed_status': 'resolved'}]
        source = render_html('问题报告', self.data)
        for fact in ('窗口内恢复问题', '窗口内已恢复', '复盘前恢复问题',
                     '窗口结束后、截至复盘时间已恢复', '历史不完整，仅展示可确认的区间',
                     '已恢复（恢复时间未记录）', '不完整历史问题', '恢复观察', '不能还原持续区间'):
            self.assertIn(fact, source)


if __name__ == '__main__':
    unittest.main()
