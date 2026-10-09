"""优惠时段边界、持久化排队、额度和项目配置回归。"""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.off_peak import ZONE, window, waiting
from autopilot.delivery import create_batch, tick

DEEPSEEK = {'provider': 'harness', 'model_provider': 'deepseek-official', 'model': 'deepseek-v4-pro'}
CODEX = {'provider': 'codex', 'model': 'fixture'}


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_weekday_boundaries(self):
        for clock, peak, resume in [('08:59:59', False, None), ('09:00:00', True, '12:00:00'),
                ('11:59:59', True, '12:00:00'), ('12:00:00', False, None),
                ('13:59:59', False, None), ('14:00:00', True, '18:00:00'),
                ('17:59:59', True, '18:00:00'), ('18:00:00', False, None)]:
            with self.subTest(clock=clock):
                state = window(at('2026-10-09T'+clock))
                self.assertEqual(state['peak'], peak)
                self.assertEqual(state['next_allowed_at'], at('2026-10-09T'+resume) if resume else None)

    def test_holidays_weekends_and_makeup_weekend(self):
        for day in ('2026-01-01', '2026-02-23', '2026-04-06', '2026-05-05',
                    '2026-06-19', '2026-09-25', '2026-10-07', '2026-10-10', '2026-10-11'):
            with self.subTest(day=day):
                self.assertFalse(window(at(day+'T10:00:00'))['peak'])
        self.assertTrue(window(at('2026-10-08T10:00:00'))['peak'])
        self.assertFalse(window(at('2027-01-01T10:00:00'))['holiday_calendar_known'])
        self.assertTrue(window(at('2027-01-01T10:00:00'))['peak'])

    def test_default_enabled_disabled_and_provider_routing(self):
        product = {'agents': {role: DEEPSEEK for role in ('discovery','implementation','verification','acceptance')}}
        for action in ('plan','develop','discover','investigate','verify','daily_attribution',
                       'daily_acceptance','daily_retrospective','review_feature','repair_release','validate_release'):
            self.assertIsNotNone(waiting(product,action,now=at('2026-10-09T10:00:00')), action)
        self.assertIsNone(waiting(product,'probe',now=at('2026-10-09T10:00:00')))
        self.assertIsNone(waiting(product,'plan',selected=CODEX,now=at('2026-10-09T10:00:00')))
        self.assertIsNone(waiting(product,'plan',selected=DEEPSEEK | {'model_provider':'third-party'},now=at('2026-10-09T10:00:00')))
        self.assertIsNone(waiting(product | {'policy':{'deepseek_off_peak_only':False}},'plan',now=at('2026-10-09T10:00:00')))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state', {'home':str(self.root/'home')})
        self.control = Control(self.store)
        self.ledger = self.control.ledger
        self.product = self.control.mutate('/products', {'config':{'name':'时段测试','source':str(self.root),
            'repository':str(self.root),'goal':'test','adapter':['fake'],'executor':['fake'],
            'agents':{'discovery':CODEX,'implementation':DEEPSEEK,'verification':CODEX,'acceptance':CODEX}}})
        self.product = self.ledger.update('products',self.product['id'],self.product['version'],{},'active')
        self.scheduler = Scheduler(self.store)

    def run_record(self, status='planning'):
        return self.ledger.create('runs',{'product_id':self.product['id'],'title':'test',
            'worker_home':str(self.root/'worker'),'workspace':str(self.root),'execution_seconds':17},status)

    def test_wait_persists_without_process_timeout_or_duplicate_updates(self):
        run = self.run_record()
        with patch('autopilot.off_peak.time.time',return_value=at('2026-10-09T10:00:00')), patch('autopilot.scheduler.subprocess.Popen') as spawn:
            self.scheduler.start_call('runs',run,self.product,'plan',['fake'])
            saved = self.ledger.get('runs',run['id'])
            restarted = Scheduler(Store(self.root/'state'))
            restarted.start_call('runs',saved,self.product,'plan',['fake'])
            self.assertEqual(self.ledger.get('runs',run['id'])['version'],saved['version'])
            spawn.assert_not_called()
            self.assertNotIn('call',saved)
            self.assertEqual(saved['execution_seconds'],17)
        with patch('autopilot.off_peak.time.time',return_value=at('2026-10-09T12:00:00')), patch('autopilot.scheduler.subprocess.Popen',return_value=Mock(pid=123)) as spawn:
            resumed = restarted.start_call('runs',saved,self.product,'plan',['fake'])
            spawn.assert_called_once()
            self.assertIsNone(resumed['off_peak_wait'])
            self.assertEqual(resumed['reason'],'')
            self.assertEqual(resumed['call']['started'],at('2026-10-09T12:00:00'))

    def test_toggle_off_while_waiting_releases_call(self):
        run = self.run_record()
        with patch('autopilot.off_peak.time.time',return_value=at('2026-10-09T10:00:00')), patch('autopilot.scheduler.subprocess.Popen',return_value=Mock(pid=123)) as spawn:
            queued = self.scheduler.start_call('runs',run,self.product,'plan',['fake'])
            updated = self.control.configure_product(self.product['id'],{'version':self.product['version'],
                'config':{'policy':self.product['policy'] | {'deepseek_off_peak_only':False}}})
            self.scheduler.start_call('runs',queued,updated,'plan',['fake'])
            spawn.assert_called_once()

    def test_queued_run_keeps_budget_and_workspace_unallocated(self):
        requirement = self.ledger.create('requirements',{'product_id':self.product['id'],'title':'test'})
        run = self.run_record('queued')
        run = self.ledger.update('runs',run['id'],run['version'],{'requirement_id':requirement['id']})
        with patch('autopilot.off_peak.time.time',return_value=at('2026-10-09T10:00:00')), patch('autopilot.scheduler.checkout') as checkout:
            self.scheduler.advance(run)
            checkout.assert_not_called()
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'queued')
        self.assertEqual(self.ledger.budget_used(self.product['id'],'development'),0)

    def test_review_wait_does_not_submit_or_start_deadline(self):
        run = self.run_record('plan_review')
        with patch('autopilot.off_peak.time.time',return_value=at('2026-10-09T10:00:00')), patch('autopilot.scheduler.Engine.submit') as submit:
            self.scheduler.review(run,self.product | {'agents':self.product['agents'] | {'acceptance':DEEPSEEK}},{})
            submit.assert_not_called()
            self.assertIsNone(self.ledger.get('runs',run['id']).get('review_id'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],0)

    def test_discovery_and_investigation_wait_before_spending_quotas(self):
        from autopilot.progress import investigate
        morning = at('2026-10-09T10:00:00')
        product = self.ledger.update('products',self.product['id'],self.product['version'],
            {'last_probe':morning,'last_inspect':morning,'agents':self.product['agents'] | {'discovery':DEEPSEEK}})
        self.ledger.signal(product['id'],{'source':'test','code':'test','summary':'test','evidence':'test'})
        requirement = self.ledger.create('requirements',{'product_id':product['id'],
            'classification':'investigation','in_scope':True},'pending')
        with patch('autopilot.off_peak.time.time',return_value=morning), patch('autopilot.scheduler.subprocess.Popen') as spawn:
            self.scheduler.monitor(product)
            investigate(self.scheduler)
            spawn.assert_not_called()
            self.assertEqual(self.ledger.budget_used(product['id'],'discovery'),0)
            self.assertEqual(self.ledger.budget_used(product['id'],'development'),0)
            self.assertEqual(self.ledger.get('requirements',requirement['id'])['status'],'pending')
        with patch('autopilot.off_peak.time.time',return_value=at('2026-10-09T12:00:00')), patch('autopilot.scheduler.subprocess.Popen',return_value=Mock(pid=123)) as spawn:
            investigate(self.scheduler)
            spawn.assert_called_once()
            self.assertEqual(self.ledger.get('requirements',requirement['id'])['status'],'investigating')
            self.assertEqual(self.ledger.budget_used(product['id'],'development'),1)

    def test_delivery_wait_does_not_spend_rounds_and_resumes_once(self):
        for flow in ('review_before_release','legacy'):
            with self.subTest(flow=flow):
                product = self.ledger.get('products',self.product['id'])
                product = self.ledger.update('products',product['id'],product['version'],{'git':{'enabled':True,'url':'https://github.com/test/test'},
                    'delivery_flow':flow,'git_migration':{'status':'completed'}})
                morning = at('2026-10-09T10:00:00')
                batch = create_batch(self.ledger,product,morning)
                batch = self.ledger.update('deliveries',batch['id'],batch['version'],{'revisions':0,'frozen':False},'repairing_feature' if flow=='review_before_release' else 'repairing')
                with patch('autopilot.scheduler.subprocess.Popen',return_value=Mock(pid=123)) as spawn:
                    tick(self.scheduler,morning)
                    tick(self.scheduler,morning)
                    self.assertEqual(self.ledger.get('deliveries',batch['id'])['revisions'],0)
                    self.assertEqual(len(self.ledger.list('code_reviews')),0)
                    spawn.assert_not_called()
                    tick(self.scheduler,at('2026-10-09T12:00:00'))
                    spawn.assert_called_once()
                    self.assertEqual(self.ledger.get('deliveries',batch['id'])['revisions'],1)
                with self.store.transaction() as db:
                    db.execute('DELETE FROM auto_deliveries')
                    db.execute('DELETE FROM auto_code_reviews')

    def test_default_and_strict_boolean_validation(self):
        self.assertTrue(self.product['policy']['deepseek_off_peak_only'])
        self.assertTrue(self.control.get('/products/'+self.product['id']+'/automation')['deepseek_schedule']['enabled'])
        for value in ('false',0,1,None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'开关必须为布尔值'):
                self.control.validate_product({'name':'test','source':str(self.root),'goal':'test','policy':{'deepseek_off_peak_only':value}})


if __name__ == '__main__':
    unittest.main()
