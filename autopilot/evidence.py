"""持久化巡查步骤、预期与实际结果、判断及截图，供控制台追溯。"""
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from .store import Ledger, redact


class Recorder:
    def __init__(self,store,product_id,scenario,method,environment='isolated'):
        self.ledger=Ledger(store)
        self.inspection=self.ledger.create('inspections',{'product_id':product_id,'title':scenario,
            'method':method,'environment':environment,'steps':[],'started_at':time.time()},'running')
        self.root=store.state/'autopilot/evidence'/self.inspection['id']
        self.root.mkdir(parents=True,mode=0o700)

    @classmethod
    def resume(cls,store,inspection_id):
        result=cls.__new__(cls)
        result.ledger=Ledger(store)
        result.inspection=result.ledger.get('inspections',inspection_id)
        result.root=store.state/'autopilot/evidence'/inspection_id
        return result

    def step(self,title,action,expected,actual,status='pass',screenshot=None,details=None):
        body=redact({'product_id':self.inspection['product_id'],'inspection_id':self.inspection['id'],
            'title':title,'action':action,'expected':expected,'actual':actual,'details':details,
            'sequence':len(self.inspection['steps'])+1,'at':time.time()})
        if screenshot:
            source=Path(screenshot)
            if source.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
                raise ValueError('截图格式无效')
            destination=self.root/(str(uuid.uuid4())+source.suffix.lower())
            shutil.copy2(source,destination)
            body.update(screenshot=str(destination),sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
        evidence=self.ledger.create('evidence',body,status)
        self.inspection=self.ledger.update('inspections',self.inspection['id'],self.inspection['version'],
            {'steps':self.inspection['steps']+[evidence['id']]})
        return evidence

    def finish(self,status,judgement):
        self.inspection=self.ledger.update('inspections',self.inspection['id'],self.inspection['version'],
            {'judgement':redact(judgement),'finished_at':time.time()},status)
        (self.root/'inspection.json').write_text(json.dumps(self.inspection,ensure_ascii=False,indent=2))
        return self.inspection


def receipt(ledger,kind,item,record):
    result=record['result']
    ident='call-'+record['call_id']
    try:
        return ledger.get('evidence',ident)
    except KeyError:
        pass
    return ledger.create('evidence',{'product_id':item['product_id'],'run_id':item['id'] if kind=='runs' else None,
        'title':record['action'],'phase':record['action'],'at':record['at'],'call_id':record['call_id'],
        'actual':result.get('summary') or result.get('reason') or result['status'],
        'judgement':result.get('reason',''),'details':redact(result),'provider':result.get('provider'),
        'model':result.get('model')},result['status'],ident=ident)
