"""扫描队列的并发去重、来源归一和历史保留回归。"""
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
from pathlib import Path
from threading import Barrier
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control


class ScanQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.control = Control(Store(self.root/'state'))
        self.ledger = self.control.ledger

    def create(self, source=None):
        return self.control.mutate('/scans', {'source': source or str(self.repo)})

    def retry(self, scan):
        return self.control.mutate('/scans/'+scan['id']+'/retry', {'version': scan['version']})

    def test_queued_running_and_unreconciled_calls_block_new_scans(self):
        scan = self.create()
        with self.assertRaises(Conflict):
            self.retry(scan)
        for status, changes in [('queued', {}), ('running', {}), ('blocked', {'call': {'id': 'pending'}})]:
            scan = self.ledger.update('scans', scan['id'], scan['version'], changes, status)
            with self.subTest(status=status), self.assertRaises(Conflict):
                self.create()
        self.assertEqual(len(self.ledger.list('scans')), 1)

    def test_local_aliases_match_existing_history_without_rewriting_it(self):
        alias = self.root/'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        original = self.create(str(alias))
        with self.assertRaises(Conflict):
            self.create(str(self.repo)+'/../repo/')
        original = self.ledger.update('scans', original['id'], original['version'], {'reason': '历史回执'}, 'pass')
        newer = self.create()
        rows = self.control.get('/scans')
        self.assertEqual(len({r['repository_key'] for r in rows}), 1)
        self.assertEqual(self.ledger.get('scans', original['id']), original)
        with self.assertRaises(Conflict):
            self.retry(original)
        newer = self.ledger.update('scans', newer['id'], newer['version'], {}, 'pass')
        retry = self.retry(original)
        self.assertEqual(retry['previous_scan_id'], original['id'])
        self.assertNotEqual(retry['id'], original['id'])
        self.assertEqual(len(self.ledger.list('scans')), 3)

    def test_remote_equivalents_match_but_other_sources_stay_separate(self):
        self.create('https://EXAMPLE.com:443/team/repo.git/')
        with self.assertRaises(Conflict):
            self.create('https://example.com/team/repo')
        self.create('https://example.com/other/repo.git')
        self.create('https://example.com:8443/team/repo.git')
        self.create()
        self.assertEqual(len(self.ledger.list('scans')), 4)

    def test_concurrent_new_and_retry_requests_create_only_one_scan(self):
        previous = self.create()
        previous = self.ledger.update('scans', previous['id'], previous['version'], {}, 'pass')
        barrier = Barrier(6)

        def submit(index):
            barrier.wait()
            try:
                return self.retry(previous) if index % 2 else self.create()
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.ledger.list('scans')), 2)

    def test_stale_retry_rejected_and_other_repository_can_queue(self):
        previous = self.create()
        self.ledger.update('scans', previous['id'], previous['version'], {}, 'pass')
        with self.assertRaises(Conflict):
            self.retry(previous)
        self.create(str(self.root/'other'))
        self.assertEqual(len(self.ledger.list('scans')), 2)


if __name__ == '__main__':
    unittest.main()
