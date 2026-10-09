"""Provider routing, discovery and durable selection tests with isolated processes."""
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import test_dashboard
from test_dashboard import wait_for
import dashboard_server as dashboard
import reviewers


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_dashboard.Fixture()
        self.fixture.setUp()
        self.store = self.fixture.store
        self.engine = self.fixture.engine

    def tearDown(self):
        self.fixture.tearDown()

    def save(self, **changes):
        cfg = self.store.settings() | changes
        with self.store.connect() as db:
            db.execute('UPDATE settings SET value=?', (json.dumps(cfg),))
        return cfg

    def test_old_config_and_stage_selection(self):
        cfg = self.store.settings()
        self.assertEqual(reviewers.snapshot(cfg, 'plan')['provider'], 'codex')
        stages = {p: {'provider':'claude','model':p} for p in reviewers.PHASES}
        cfg = self.save(reviewer_mode='stages', reviewer_stages=stages)
        reviewers.validate_config(cfg)
        for p in reviewers.PHASES:
            self.assertEqual(reviewers.snapshot(cfg,p)['model'],p)
        with self.assertRaises(ValueError):
            reviewers.validate_config(cfg | {'reviewer_stages':{'plan': stages['plan']}})
        with self.assertRaises(ValueError):
            reviewers.selection({'provider':'harness','model':'m'})

    def test_duplicate_request_retains_snapshot_after_settings_change(self):
        packet = self.fixture.packet()
        rid = self.engine.submit(packet, 'observation')
        old = self.store.get(rid)['reviewer']
        self.save(reviewer_unified={'provider':'claude','model':'different'})
        self.assertEqual(self.engine.submit(packet,'observation'),rid)
        self.assertEqual(self.store.get(rid)['reviewer'],old)
        wait_for(lambda:self.store.get(rid)['execution_done'])
        self.assertEqual(self.store.get(rid)['result']['decision'],'approve')

    def test_claude_and_harness_results_and_phase_rules(self):
        for provider,phase,decision in [('claude','plan','approve'),('harness','acceptance','done')]:
            with self.subTest(provider=provider):
                fake = self.fixture.root / ('fake-'+provider)
                result = {'decision':decision,'summary':'verified','instruction':'next','checks':[],'issues':[]}
                output = {'structured_output':result,'is_error':False} if provider=='claude' else {'type':'final','text':json.dumps(result)}
                fake.write_text(f'#!{sys.executable}\nimport sys,json\nsys.stdin.read()\nprint({json.dumps(output)!r})\n')
                fake.chmod(0o700)
                selection={'provider':provider,'model':'test',**({'model_provider':'test-route'} if provider=='harness' else {})}
                self.save(**{provider+'_bin':str(fake),'reviewer_unified':selection})
                packet=self.fixture.packet();packet['phase']=phase
                rid=self.engine.submit(packet,'observation')
                row=wait_for(lambda: self.store.get(rid) if self.store.get(rid)['execution_done'] else None)
                self.assertEqual(row['result']['decision'],decision,row['result'])
                self.assertEqual(row['reviewer']['provider'],provider)
                self.assertEqual(json.loads((self.store.state/'reviews'/rid/'reviewer.json').read_text())['model'],'test')

    def test_malformed_and_permission_denied_fail_closed(self):
        fake=self.fixture.root/'fake-claude'
        for output in ['not-json',json.dumps({'structured_output':{'decision':'approve'},'permission_denials':[{'tool':'Read'}]})]:
            fake.write_text(f'#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint({output!r})\n');fake.chmod(0o700)
            self.save(claude_bin=str(fake),reviewer_unified={'provider':'claude','model':'test'})
            rid=self.engine.submit(self.fixture.packet(),'observation')
            wait_for(lambda:self.store.get(rid)['execution_done'])
            self.assertEqual(self.store.get(rid)['result']['decision'],'blocked')

    def test_catalog_cache_failure_and_no_quota(self):
        helper=self.fixture.root/'catalog.py'
        helper.write_text('import sys,json\nsys.stdin.read()\nprint(json.dumps([{"id":"dynamic-model","name":"Live model"}]))')
        cfg=self.save(node_bin=sys.executable,reviewer_helper=str(helper))
        value=reviewers.catalog(cfg,self.store.state,'claude')
        self.assertFalse(value['stale']);self.assertEqual(value['models'][0]['id'],'dynamic-model')
        helper.write_text('raise SystemExit(2)')
        cached=reviewers.catalog(cfg,self.store.state,'claude',True)
        self.assertTrue(cached['stale']);self.assertTrue(cached['error']);self.assertEqual(cached['models'],value['models'])
        empty=reviewers.catalog(cfg | {'claude_bin':'another-cli'},self.store.state,'claude',True)
        self.assertEqual(empty['models'],[])
        self.assertFalse((self.store.state/'quota.json').exists())

    def test_harness_uses_separate_credentials_home_and_session_storage(self):
        cfg=self.store.settings() | {'harness_home':'/configured-harness-home','reviewer_unified':{'provider':'harness','model':'m','model_provider':'p'}}
        selected=reviewers.snapshot(cfg,'plan')
        cmd,env=reviewers.harness_command(selected,self.fixture.root)
        self.assertEqual(selected['home'],str(self.fixture.home))
        self.assertEqual(env['DSH_HOME'],'/configured-harness-home')
        self.assertEqual(env['DSH_SUPERVISOR_REVIEW'],'run')
        overlay=json.loads((self.fixture.root/'reviewer.patch.json').read_text())
        self.assertEqual(next(x for x in overlay if x.get('id')=='session-persistence-jsonl')['config']['root'],str(self.fixture.root/'sessions'))

    def test_settings_validation_and_preserve_modes(self):
        app=dashboard.Dashboard(self.store.state)
        cfg=app.mutate('/settings',{'reviewer_unified':{'provider':'claude','model':'opus'}})
        self.assertEqual(cfg['reviewer_unified']['model'],'opus')
        with self.assertRaises(ValueError):app.mutate('/settings',{'reviewer_mode':'stages'})
        stages={p:{'provider':'codex','model':'gpt-test'} for p in reviewers.PHASES}
        app.mutate('/settings',{'reviewer_mode':'stages','reviewer_stages':stages})
        cfg=app.mutate('/settings',{'reviewer_mode':'unified'})
        self.assertEqual(cfg['reviewer_stages'],stages)
        self.assertEqual(cfg['reviewer_unified']['provider'],'claude')
        app.engine.stop()
