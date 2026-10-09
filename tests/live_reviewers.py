"""Opt-in paid reviewer smoke test; creates an explicitly labelled independent fixture session."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'dsh-gpt-supervisor/scripts'))
from review_core import Store, Engine


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',action='store_true',required=True)
    parser.add_argument('--home',required=True)
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--harness-bin',required=True)
    parser.add_argument('--providers',nargs='+',choices=['codex','claude','harness'],default=['codex','claude','harness'])
    args=parser.parse_args()
    root=Path(args.state_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    project=root/'fixture';project.mkdir(exist_ok=True)
    (project/'add.py').write_text('def add(a, b):\n    return a + b\n')
    (project/'test_add.py').write_text('import unittest\nfrom add import add\nclass AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n        self.assertEqual(add(-2, 2), 0)\n')
    sid='session-'+str(uuid.uuid4())
    fixture_home=root/'home'
    log=fixture_home/'sessions'/'supervisor-reviewer-verification'/sid/'session.jsonl'
    log.parent.mkdir(parents=True,exist_ok=True)
    log.write_text('\n'.join(json.dumps(row,ensure_ascii=False) for row in [
        {'seq':0,'type':'session/header','id':sid,'cwd':str(project)},
        {'seq':1,'type':'session/title','data':{'title':'独立审查器验证（测试夹具）'}},
    ])+'\n')
    store=Store(root,{'home':str(fixture_home),'harness_home':args.home,'harness_bin':args.harness_bin})
    engine=Engine(store); results=[]
    try:
        for provider,model in [('codex','gpt-5.5'),('claude','default'),('harness','deepseek-v4-pro')]:
            if provider not in args.providers:continue
            selection={'provider':provider,'model':model,**({'model_provider':'deepseek-official'} if provider=='harness' else {})}
            cfg=store.settings()|{'reviewer_unified':selection,'harness_bin':args.harness_bin}
            with store.connect() as db:db.execute('UPDATE settings SET value=?',(json.dumps(cfg),))
            rid=str(uuid.uuid4())
            engine.submit({'request_id':rid,'session_id':sid,'cwd':str(project),'phase':'acceptance','scope':['add.py','test_add.py'],'pause_seq':1,
                'summary':'这是独立审查器验收测试夹具，不是历史开发会话。任务：核对 add.py 实现加法，test_add.py 覆盖正数及相反数。仅可读取这两个文件，前台执行 python3 -m unittest -v test_add.py（命令超时不超过30秒）。源代码和会话保持不变。依据实际文件和测试返回 done/revise；工具故障如实 blocked，不要新增需求或修改文件。'},'handoff')
            print(json.dumps({'provider':provider,'review_id':rid,'status':'submitted'}),flush=True)
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                row=store.get(rid)
                if row['execution_done']:
                    results.append({'provider':provider,'review_id':rid,'result':row['result']})
                    (root/'verification.json').write_text(json.dumps({'session_id':sid,'reviews':results},ensure_ascii=False,indent=2))
                    print(json.dumps({'provider':provider,'result':row['result']},ensure_ascii=False),flush=True)
                    break
                time.sleep(.5)
            else:raise RuntimeError('验证等待超时')
    finally:engine.stop()

if __name__=='__main__':main()
