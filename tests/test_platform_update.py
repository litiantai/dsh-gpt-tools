"""平台更新状态、手动解除的版本绑定、健康门槛与幂等回归。"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.call import atomic
from autopilot.platform_update import status, request_release
from dashboard_server import Dashboard
from review_core import Conflict

spec = importlib.util.spec_from_file_location('updater_control_test', ROOT/'scripts/platform-updater.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
atomic = updater.atomic


class PlatformUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.auto = self.state/'autopilot'
        self.job = self.auto/'updates/fixture.json'
        self.config = {'state': str(self.state), 'observation_seconds': 1800}
        atomic(self.state/'platform/updater/config.json', self.config)
        artifact = self.auto/'candidates/fixture/artifact'; artifact.mkdir(parents=True)
        manifest = artifact.parent/'manifest.json'
        atomic(manifest, {'artifact': str(artifact), 'product_id': 'fixture', 'commit': 'head',
                          'database_compatibility': 'backward-compatible'})
        atomic(self.job, {'status': 'observing', 'product_id': 'fixture', 'expected_commit': 'head',
                          'manifest': str(manifest), 'healthy_since': time.time()-60})
        atomic(self.auto/'update-drain.json', {'job': str(self.job)})
        self.body = {'job_id': 'fixture', 'commit': 'head', 'operation_id': str(uuid.uuid4())}

    def tearDown(self):
        self.temp.cleanup()

    def test_status_exposes_wait_impact_and_countdown(self):
        value = status(self.state)
        self.assertTrue(value['active']); self.assertTrue(value['can_release'])
        self.assertIn('任务派发', value['impact'])
        self.assertGreater(value['remaining_seconds'], 1700)
        self.assertEqual(value['label'], '更新观察中')

    def test_only_observation_and_matching_version_can_release(self):
        with self.assertRaises(Conflict): request_release(self.state, self.body | {'commit': 'old'})
        row = json.loads(self.job.read_text()); row['status'] = 'switching'; atomic(self.job, row)
        with self.assertRaises(Conflict): request_release(self.state, self.body)
        self.assertFalse(status(self.state)['can_release'])

    def test_authenticated_operation_allowed_during_drain_and_idempotent(self):
        app = Dashboard(self.state)
        with self.assertRaises(Conflict): app.operation('/products/fixture/enable', self.body)
        first = app.operation('/platform-update/release', self.body)
        self.assertTrue(first['release_requested'])
        self.assertEqual(first, app.operation('/platform-update/release', self.body))
        self.assertTrue((self.auto/'update-drain.json').exists())

    def test_updater_releases_only_after_health_pass_and_records_manual_reason(self):
        request_release(self.state, self.body)
        with patch.object(updater, 'healthy', return_value=True): updater.step(self.config, self.job)
        row = json.loads(self.job.read_text())
        self.assertEqual(row['status'], 'completed')
        self.assertTrue(row['manual_observation_release']['health_verified'])
        self.assertIn('手动', row['reason'])
        self.assertFalse(status(self.state)['active'])

    def test_health_failure_does_not_complete_or_claim_manual_success(self):
        request_release(self.state, self.body)
        with patch.object(updater, 'healthy', return_value=False): updater.step(self.config, self.job)
        row = json.loads(self.job.read_text())
        self.assertNotEqual(row['status'], 'completed')
        self.assertNotIn('manual_observation_release', row)

    def test_stale_request_does_not_shorten_another_version(self):
        request_release(self.state, self.body)
        p = self.auto/'update-requests/fixture.json'; row = json.loads(p.read_text()); row['expected_commit'] = 'old'; atomic(p, row)
        with patch.object(updater, 'healthy', return_value=True): updater.step(self.config, self.job)
        self.assertEqual(json.loads(self.job.read_text())['status'], 'observing')
        self.assertTrue((self.auto/'update-drain.json').exists())
