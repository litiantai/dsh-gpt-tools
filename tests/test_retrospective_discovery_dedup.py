"""复盘入池后再次晚间归因必须复用显式 ID，即使标题变化且无新信号。"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.scheduler import Scheduler


class DiscoveryDedupTests(unittest.TestCase):
    def test_existing_id_title_change_and_foreign_id_do_not_create_duplicate_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler=Scheduler(Store(Path(temp)/'state'));ledger=scheduler.ledger
            product=ledger.create('products',{'name':'复盘项目'},'paused')
            signal=ledger.create('signals',{'product_id':product['id'],'summary':'同一个问题'},'pending')
            existing=ledger.create('requirements',{'product_id':product['id'],'title':'原始标题',
                'signal_ids':[signal['id']],'classification':'investigation','daily_report_id':'report'},'pending')
            candidate={'title':'另一个标题','existing_requirement_id':existing['id'],'signal_ids':[signal['id']],
                'classification':'investigation','in_scope':True,'evidence':'真实证据','impact':'影响研发',
                'acceptance':['补齐证据'],'reproduction':'运行相同检查'}
            first=scheduler.accept_discovery(product,{'requirements':[candidate]},[signal['id']],report_id='report')
            second=scheduler.accept_discovery(product,{'requirements':[candidate]},[signal['id']],report_id='report')
            self.assertEqual([row['id'] for row in first],[existing['id']])
            self.assertEqual([row['id'] for row in second],[existing['id']])
            scheduler.accept_discovery(product,{'requirements':[candidate|{'existing_requirement_id':'foreign'}]},[signal['id']])
            self.assertEqual(len(ledger.scoped('requirements',product['id'])),1)


if __name__=='__main__':unittest.main()
