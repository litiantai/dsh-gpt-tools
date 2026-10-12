"""实际源码工作区、开发分支核对及两种模型执行器的分支选择回归。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.workspace import execution_source, git


class ExecutionSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        git(self.repo,'init','-b','master')
        git(self.repo,'config','user.name','Test')
        git(self.repo,'config','user.email','test@localhost')
        (self.repo/'source.txt').write_text('master')
        git(self.repo,'add','.');git(self.repo,'commit','-m','master')
        self.feature = self.root/'feature'
        git(self.repo,'worktree','add','-b','feat-candidate',str(self.feature))
        (self.feature/'source.txt').write_text('candidate')
        git(self.feature,'commit','-am','candidate')
        self.request = {'product':{'id':'project','source':str(self.repo),'repository':str(self.repo),
                                  'test_execution':'local','goal':'修复候选缺陷'},
                        'record':{'id':'candidate','workspace':str(self.feature),'branch':'feat-candidate'},
                        'state_root':str(self.root/'state/autopilot')}

    def test_task_stages_read_candidate_and_report_actual_commit(self):
        with patch('autopilot.master.source_workspace',side_effect=AssertionError('读取了 master')):
            for action in ('plan','develop','validate','design_ui','design_api','computer_step'):
                source=execution_source(action,self.request)
                self.assertEqual(source,{'workspace':str(self.feature),'branch':'feat-candidate',
                                         'commit':git(self.feature,'rev-parse','HEAD')})
                self.assertEqual((Path(source['workspace'])/'source.txt').read_text(),'candidate')

    def test_master_only_stages_still_use_master(self):
        with patch('autopilot.master.source_workspace',return_value=str(self.repo)) as select:
            for action in ('discover','investigate','daily_acceptance','daily_retrospective','daily_attribution','coordinate'):
                self.assertEqual(execution_source(action,self.request)['branch'],'master')
            self.assertEqual(select.call_count,6)

    def test_missing_or_wrong_task_workspace_is_rejected(self):
        for workspace in (None,str(self.repo)):
            for action in ('plan','develop','validate'):
                request=self.request | {'record':self.request['record'] | {'workspace':workspace}}
                with self.subTest(action=action,workspace=workspace), self.assertRaisesRegex(ValueError,'开发分支'):
                    execution_source(action,request)

    def test_harness_plan_and_development_launch_in_candidate(self):
        from autopilot.executor import execute
        runtime=self.root/'runtime'
        package=runtime/'node_modules/@deepseek-ai/dsh';package.mkdir(parents=True)
        (package/'package.json').write_text(json.dumps({'bin':'fixture.mjs'}))
        (package/'fixture.mjs').write_text('''
import fs from 'node:fs';
const prompt=fs.readFileSync(0,'utf8');
if(fs.readFileSync('source.txt','utf8')!=='candidate')throw Error('wrong workspace');
if(!prompt.includes('"branch": "feat-candidate"'))throw Error('missing source identity');
const result={status:'pass',reason:'',plan:'修复候选测试',summary:'候选源码已核对'};
console.log(JSON.stringify({type:'final',text:JSON.stringify(result)}));
''')
        product=self.request['product'] | {'worker_runtime':str(runtime),'node':shutil.which('node'),
            'adapter_spec':{'version':1,'kind':'command','capabilities':['verify']},
            'model_source':{'home':str(self.root/'fake-home'),'profile':'fixture'},
            'agents':{'implementation':{'provider':'harness','model_provider':'fixture','model':'fixture'}}}
        original_run=subprocess.run
        def run(argv,**kwargs):
            if any(str(arg).endswith('autopilot-profile.mjs') for arg in argv):
                return subprocess.CompletedProcess(argv,0)
            return original_run(argv,**kwargs)
        with patch('autopilot.executor.subprocess.run',side_effect=run), \
             patch('autopilot.executor.restrict',side_effect=lambda argv,*args,**kwargs:argv), \
             patch('autopilot.master.source_workspace',side_effect=AssertionError('读取了 master')):
            for action in ('plan','develop'):
                result=execute(action,self.request | {'product':product})
                self.assertEqual(result['status'],'pass',result)
                self.assertEqual(result['source_context']['branch'],'feat-candidate')
                self.assertEqual(json.loads((Path(result['evidence'])/'source.json').read_text()),result['source_context'])
