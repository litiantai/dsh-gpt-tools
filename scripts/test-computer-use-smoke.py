#!/usr/bin/env python3
"""真实 Codex 图像验收：隔离样例分支中的通过、基准、失败及修复复验。"""
import argparse
import copy
import json
from pathlib import Path
import sys
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.workspace import git
from autopilot.test_chains import enqueue_generation, make_run, revise
from autopilot.test_chain_worker import generate, finish_generation
from autopilot.local_testing import verification
from autopilot.computer_use import execute_run

HTML='''<!doctype html><html lang="zh"><meta charset="utf-8"><title>报告测试</title><style>body{font:24px sans-serif;margin:90px}button,input{font:24px sans-serif;padding:18px;margin:15px}#result{padding:30px;color:#176936}</style><h1>报告生成</h1><label>报告名称 <input id="name" aria-label="报告名称"></label><button id="submit" onclick="this.disabled=true;document.getElementById('result').textContent='生成中';setTimeout(()=>{document.getElementById('result').textContent='报告完成：'+document.getElementById('name').value},1200)">生成报告</button><div id="result">尚未生成</div>FEATURE</html>'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
    repo=root/'fixture';repo.mkdir();(repo/'index.html').write_text(HTML.replace('FEATURE',''))
    git(repo,'init','-b','feat-computer-smoke');git(repo,'config','user.name','Computer Use Test');git(repo,'config','user.email','test@localhost')
    git(repo,'add','.');git(repo,'commit','-m','baseline fixture')
    control=Control(Store(root/'state'));ledger=control.ledger
    product=ledger.create('products',{'name':'Computer Use 实测','source':str(repo),'repository':str(repo),'goal':'报告生成验收',
        'worker_runtime':str(ROOT), 'agents':{'verification':{'provider':'codex','model':args.model,'reasoning_effort':'low'}},
        'computer_use':{'enabled':True,'interval_seconds':1,'timeout_seconds':600,'max_observations':20,'max_actions':25},
        'project_config':{'commands':{'start':[[sys.executable,'-m','http.server','{port}','--bind','127.0.0.1']]},'startup_timeout':5}},'paused')
    requirement=ledger.create('requirements',{'product_id':product['id'],'title':'生成报告','goal':'模拟用户生成报告',
        'scope':'报告测试网页','scenario':'在首页输入季度总结并点击生成报告','acceptance':['页面显示“报告完成：季度总结”'],
        'confirmation_status':'confirmed','confirmation_required':True},'queued')
    chain=enqueue_generation(ledger,product,requirement)
    request={'state_root':str(root/'state/autopilot'),'product':product,'record':chain,'requirement':requirement}
    result=generate(request);(root/'generation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    chain=finish_generation(ledger,chain,result)
    if chain['status']!='ready':raise RuntimeError(chain.get('reason'))
    print('真实 Codex 已生成链路',flush=True)
    outcomes=[]
    def run(label):
        record={'id':str(uuid.uuid4()),'title':label,'workspace':str(repo),'branch':'feat-computer-smoke','commit':git(repo,'rev-parse','HEAD'),'requirement_id':requirement['id']}
        req=request|{'record':record};execution=make_run(ledger,product,[chain],record)
        with verification(req,root/label) as checked:
            if checked['status']!='pass':raise RuntimeError(checked['reason'])
            result=execute_run(req,execution,checked['instance'])
        outcomes.append({'label':label,**result});(root/'results.json').write_text(json.dumps(outcomes,ensure_ascii=False,indent=2))
        print(label+': '+json.dumps(result,ensure_ascii=False),flush=True)
        return ledger.get('test_chain_runs',execution['id'])
    baseline=run('baseline')
    if baseline['status']!='pass':raise RuntimeError('基准运行未通过')
    prefix='/products/'+product['id']+'/test-chains'
    control.mutate(prefix+'/runs/'+baseline['id']+'/confirm-baseline',{'version':baseline['version']})
    chain=ledger.get('test_chains',chain['id'])
    definition=copy.deepcopy(ledger.get('test_chain_versions',chain['current_version_id'])['definition'])
    definition['steps'].append({'id':'download','goal':'观察新增的下载报告按钮，不点击、不重新提交；如果不存在则判定失败',
        'expected':'页面可见“下载报告”按钮','loop':False,'observation_action':'none','acceptance_indices':[]})
    chain=revise(ledger,chain,definition,requirement['id'])
    failed=run('new-expectation')
    if failed['status']!='fail':raise RuntimeError('新增能力缺失未正确判为失败')
    (repo/'index.html').write_text(HTML.replace('FEATURE','<button>下载报告</button>'))
    git(repo,'commit','-am','implement download capability')
    passed=run('fixed')
    if passed['status']!='pass':raise RuntimeError('修复复验未通过')
    print('完整实测通过；证据保留在 '+str(root),flush=True)

if __name__=='__main__':main()
