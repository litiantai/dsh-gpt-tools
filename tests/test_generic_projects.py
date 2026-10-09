"""通用接入、隔离执行、兼容迁移和独立更新恢复的行为测试。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts'), str(ROOT/'tests')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.project import migrate, manifest_hash
from autopilot.onboarding import detect, execute, startup, verify, onboard
from autopilot.workspace import git, snapshot
from autopilot.isolated import command, environment, independent_runtime, run
from autopilot.probes import validate

spec=importlib.util.spec_from_file_location('updater', ROOT/'scripts/platform-updater.py')
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)


class GenericTests(unittest.TestCase):
    def test_worker_and_review_profiles_disable_nested_sandbox(self):
        home=self.root/'model-source'
        profile=home/'profiles/fixture';profile.mkdir(parents=True)
        (profile/'cordis.patch.yml').write_text('- id: agent-default-model\n  config:\n    provider: fixture\n    model: test\n')
        dest=self.root/'model-home'
        subprocess.run(['node',str(ROOT/'scripts/autopilot-profile.mjs'),str(home),'fixture',str(dest),str(self.root/'runtime'),str(ROOT/'scripts/autopilot-guard.mjs')],check=True,capture_output=True)
        for name in ('autopilot-worker','autopilot-review'):
            contents=(dest/'profiles'/name/'cordis.patch.yml').read_text()
            self.assertIn('mode: danger-full-access',contents)
            self.assertIn('policy: never',contents)
        self.assertNotIn('autopilot-guard',(dest/'profiles/autopilot-review/cordis.patch.yml').read_text())

    def test_model_environment_excludes_inherited_service_credentials(self):
        from autopilot.sandbox import model_environment
        with patch.dict(os.environ, {'PRODUCTION_DATABASE_PASSWORD':'synthetic-fixture'}):
            self.assertNotIn('PRODUCTION_DATABASE_PASSWORD', model_environment())
            self.assertEqual(model_environment({'DSH_HOME':'isolated'})['DSH_HOME'], 'isolated')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.repo=self.root/'repo';self.repo.mkdir()
        git(self.repo,'init');git(self.repo,'config','user.name','Test');git(self.repo,'config','user.email','test@localhost')
        (self.repo/'README.md').write_text('fixture')
        git(self.repo,'add','.');git(self.repo,'commit','-m','fixture')
        self.store=Store(self.root/'state');self.control=Control(self.store);self.ledger=self.control.ledger
        self.p=self.ledger.create('products',{'name':'fixture','source':str(self.repo),'goal':'fixture'},'paused')

    def tearDown(self):
        self.tmp.cleanup()

    def test_detection_node_and_ambiguous_start(self):
        (self.repo/'package.json').write_text(json.dumps({'scripts':{'build':'tsc','test':'test'}}))
        (self.repo/'package-lock.json').write_text('{}')
        config=detect(self.repo)
        self.assertEqual(config['commands']['install'][0][1], 'ci')
        self.assertEqual(config['commands']['test'], [['npm','run','test']])
        self.assertEqual(config['install_evidence']['package_lock_sha256'], __import__('hashlib').sha256(b'{}').hexdigest())
        self.assertNotIn('unpinned', config['install_evidence'])
        self.assertTrue(config['blockers'])

    def test_independent_runtime_follows_state_root(self):
        state=self.root/'custom-state'
        runtime=state/'autopilot/runtime'
        from test_runtime_install import fixture
        fixture(runtime)
        self.assertEqual(independent_runtime(state/'autopilot'), runtime)
        env=environment(state/'execution',runtime=runtime)
        self.assertEqual(env['DSH_RUNTIME_NODE_MODULES'],str(runtime/'node_modules'))
        if sys.platform=='darwin':
            root=state/'execution';root.mkdir(parents=True,exist_ok=True)
            command([sys.executable,'-c','pass'],root,self.repo,runtime=runtime)
            self.assertIn(str(runtime),(root/'execute.sb').read_text())
        # 显式传入无效运行时不静默回退，避免注入非固定运行时
        invalid=environment(state/'execution2',runtime=state/'missing-runtime')
        self.assertNotIn('DSH_RUNTIME_NODE_MODULES',invalid)

    def test_unlocked_npm_is_blocked_unless_explicitly_configured(self):
        (self.repo/'package.json').write_text(json.dumps({'scripts':{'start':'node server.js'}}))
        config=detect(self.repo)
        self.assertNotIn('install',config['commands'])
        self.assertIsNone(config['install_evidence']['package_lock_sha256'])
        blocker=next((b for b in config['blockers'] if 'package-lock.json' in b),None)
        self.assertIsNotNone(blocker)
        blocked=verify(self.repo,self.root/'execution',config)
        self.assertEqual(blocked['status'],'blocked')
        self.assertIn('package-lock.json',blocked['reason'])
        self.assertEqual(blocked['checks'],[])
        # 运行守卫同样拒绝未固化且未显式声明的 npm install
        guard={'version':1,'commands':{'install':[['npm','install','--ignore-scripts','--no-audit','--no-fund']]},'blockers':[]}
        guarded=verify(self.repo,self.root/'execution',guard)
        self.assertEqual(guarded['status'],'blocked')
        self.assertIn('install_policy',guarded['reason'])
        self.assertEqual(guarded['checks'],[])
        # 显式声明可复现安装策略后解除默认阻断并保留命令
        (self.repo/'.autopilot.json').write_text(json.dumps({'version':1,'install_policy':'explicit-unpinned',
            'commands':{'install':[['npm','install','--ignore-scripts','--no-audit','--no-fund']],
                        'start':[['npm','run','start']]}}))
        explicit=detect(self.repo)
        self.assertNotIn(blocker,explicit['blockers'])
        self.assertEqual(explicit['install_policy'],'explicit-unpinned')
        self.assertTrue(explicit['install_evidence']['unpinned'])
        self.assertEqual(explicit['commands']['install'][0][:2],['npm','install'])

    def test_unlocked_npm_explicit_install_command_lifts_blocker(self):
        (self.repo/'package.json').write_text(json.dumps({'scripts':{'start':'node server.js'}}))
        (self.repo/'.autopilot.json').write_text(json.dumps({'version':1,
            'commands':{'install':[['npm','install','--ignore-scripts','--no-audit','--no-fund']],
                        'start':[['npm','run','start']]}}))
        config=detect(self.repo)
        self.assertFalse([b for b in config['blockers'] if 'package-lock.json' in b])
        self.assertEqual(config['install_policy'],'explicit-unpinned')
        self.assertEqual(config['commands']['install'][0][:2],['npm','install'])

    def test_python_and_jvm_are_detected_without_guessing(self):
        for name,stack in [('pyproject.toml','python'),('pom.xml','java')]:
            (self.repo/name).write_text('')
            self.assertIn(stack,detect(self.repo)['stacks'])
        self.assertTrue(detect(self.repo)['blockers'])

    def test_scan_retry_keeps_history_and_does_not_apply_config(self):
        scan=self.control.mutate('/scans',{'source':str(self.repo),'product_id':self.p['id']})
        scan=self.ledger.update('scans',scan['id'],scan['version'],{'reason':'missing dependencies'},'blocked')
        newer=self.control.mutate('/scans/'+scan['id']+'/retry',{'version':scan['version']})
        self.assertNotEqual(scan['id'],newer['id'])
        self.assertEqual(newer['previous_scan_id'],scan['id'])
        self.assertEqual(self.ledger.get('scans',scan['id'])['reason'],'missing dependencies')
        self.assertNotIn('project_config',self.ledger.get('products',self.p['id']))

    def test_onboard_persists_project_link_and_rejects_cross_project_apply(self):
        scan=self.control.mutate('/scans',{'source':str(self.repo)})
        scan=self.ledger.update('scans',scan['id'],scan['version'],{'result':{'configuration':detect(self.repo),'workspace':str(self.repo),'snapshot_commit':git(self.repo,'rev-parse','HEAD')}},'pass')
        linked=onboard(self.ledger,scan,{})
        product=self.ledger.get('products',linked['product_id'])
        self.assertEqual(product['config_scan_id'],scan['id'])
        self.assertEqual(self.ledger.get('scans',scan['id'])['product_id'],product['id'])
        other=self.ledger.create('products',{k:v for k,v in product.items() if k not in ('id','product_id')},'observing')
        with self.assertRaises(Conflict):
            self.control.mutate('/scans/'+scan['id']+'/apply',{'version':linked['version'],'product_id':other['id'],'product_version':other['version']})
        remote=self.ledger.create('scans',{'source':'https://example.com/other.git','result':scan['result']},'pass')
        for target in (self.p,product):
            with self.assertRaises(Conflict):
                self.control.mutate('/scans/'+remote['id']+'/apply',{'version':remote['version'],'product_id':target['id'],'product_version':target['version']})

    def test_model_provider_result_is_attributed_without_fallback(self):
        from autopilot.codex_executor import execute as model
        fake=self.root/'model'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","reason":"","summary":"verified","plan":"implement"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(self.repo),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}}}
        result=model('plan',{'product':product,'record':{},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['provider'],'codex');self.assertEqual(result['plan'],'implement')

    def worktree_fixture(self):
        """真实 bare repo + `git worktree add`：checkout 与 git 元数据分离。"""
        private=self.root/'private';private.mkdir()
        bare=private/'repository.git'
        git(private,'init','--bare',str(bare))
        seed=self.root/'seed';seed.mkdir()
        git(seed,'init');git(seed,'config','user.name','Test');git(seed,'config','user.email','test@localhost')
        (seed/'app.py').write_text('base\n')
        git(seed,'add','.');git(seed,'commit','-m','fixture')
        base=git(seed,'rev-parse','HEAD')
        git(seed,'remote','add','origin',str(bare));git(seed,'push','origin','HEAD:refs/heads/main')
        workspace=private/'linked'
        git(bare,'worktree','add','-b','release',str(workspace),base)
        git(workspace,'config','user.name','Test');git(workspace,'config','user.email','test@localhost')
        (workspace/'app.py').write_text('base\nchange\n')
        git(workspace,'add','.');git(workspace,'commit','-m','change the app')
        return workspace,bare,base,git(workspace,'rev-parse','HEAD')

    def test_generic_codex_uses_outer_sandbox_for_tool_execution(self):
        from autopilot.codex_executor import execute as model
        workspace,bare,base,head=self.worktree_fixture()
        fake=self.root/'codex-model'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\nassert sys.argv[sys.argv.index("--sandbox")+1]=="danger-full-access"\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","plan":"outer policy"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        with patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv) as sandbox:
            result=model('plan',{'product':product,'record':{'workspace':str(workspace)},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass')
        self.assertTrue(sandbox.call_args.kwargs['deny_local'])
        self.assertNotIn(str(Path(workspace).resolve()),[str(Path(p).resolve()) for p in sandbox.call_args.args[1]])
        # The worktree metadata (HEAD/index/commondir/objects) must be readable for
        # base..head diffing, while staying outside the write set.
        read_allowed=[str(Path(p).resolve()) for p in sandbox.call_args.kwargs['read_allowed']]
        self.assertIn(str((bare/'worktrees'/'linked').resolve()),read_allowed)
        self.assertIn(str(bare.resolve()),read_allowed)
        self.assertIn(str(Path(workspace).resolve()),[str(Path(p).resolve()) for p in sandbox.call_args.kwargs['readonly_roots']])

    def test_validate_sandbox_reads_worktree_git_metadata_without_write(self):
        from autopilot.codex_executor import execute as model
        from autopilot.workspace import metadata
        workspace,bare,base,head=self.worktree_fixture()
        git_dir,common=metadata(workspace)
        # A relative `--git-common-dir` must resolve to the private bare root, while
        # HEAD/index/commondir stay in the linked worktree git dir.
        self.assertEqual(Path(git_dir),(bare/'worktrees'/'linked').resolve())
        self.assertEqual(Path(common),bare.resolve())
        fake=self.root/'codex-validate'
        fake.write_text('#!'+sys.executable+'\nimport os,sys,json\nfrom pathlib import Path\nassert os.environ.get("GIT_CONFIG_GLOBAL")=="/dev/null"\nassert os.environ.get("GIT_CONFIG_NOSYSTEM")=="1"\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","summary":"validated"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        with patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv) as sandbox:
            result=model('validate',{'product':product,'record':{'id':'round','workspace':str(workspace),'base_commit':base,'commit':head},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        writable=[str(Path(p).resolve()) for p in sandbox.call_args.args[1]]
        read_allowed=[str(Path(p).resolve()) for p in sandbox.call_args.kwargs['read_allowed']]
        readonly=[str(Path(p).resolve()) for p in sandbox.call_args.kwargs['readonly_roots']]
        self.assertIn(str(Path(git_dir).resolve()),read_allowed)
        self.assertIn(str(Path(common).resolve()),read_allowed)
        self.assertNotIn(str(Path(workspace).resolve()),writable)
        self.assertIn(str(Path(workspace).resolve()),readonly)
        self.assertIn(str(Path(git_dir).resolve()),readonly)
        # 裸仓库 config 必须可读（供 --git-dir 差异回退），且绝不进入可写集合。
        self.assertIn(str((bare/'config').resolve()),read_allowed)
        self.assertNotIn(str((bare/'config').resolve()),writable)
        # The controller writes the real base..commit evidence into the readable
        # execution directory instead of making the verifier read git metadata.
        root=Path(result['evidence'])
        text=(root/'verification-diff.patch').read_text()
        facts=json.loads((root/'verification-facts.json').read_text())
        self.assertIn('+change',text)
        self.assertEqual((facts['base_commit'],facts['commit'],facts['merge_base']),(base,head,base))
        self.assertEqual([line.split('\t')[-1] for line in facts['changed']],['app.py'])

    def test_validate_inlines_controller_diff_without_git_metadata_access(self):
        """验证者只读控制器内联的差异即可判断，不再依赖 checkout 外的 git 元数据。"""
        from autopilot.codex_executor import execute as model
        workspace,bare,base,head=self.worktree_fixture()
        fake=self.root/'codex-evidence-only'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'assert "verification-diff.patch" in prompt\n'
            'assert "+change" in prompt\n'
            'Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","summary":"used controller diff only"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        with patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv):
            result=model('validate',{'product':product,'record':{'workspace':str(workspace),'base_commit':base,'commit':head},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        root=Path(result['evidence'])
        self.assertTrue((root/'verification-diff.patch').is_file())
        self.assertTrue((root/'verification-facts.json').is_file())

    def test_validate_falls_back_to_bare_repository_when_worktree_metadata_unreadable(self):
        """checkout 的 Git 元数据不可读时，显式下发的 bare 仓库仍能生成 base..commit 差异。"""
        from autopilot.codex_executor import execute as model
        workspace,bare,base,head=self.worktree_fixture()
        # 移走 worktree 指针，使 `git -C workspace` 无法解析；只有 bare 仓库可用。
        (workspace/'.git').rename(self.root/'displaced-worktree-git')
        fake=self.root/'codex-bare-fallback'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'assert "+change" in prompt\n'
            'Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","summary":"bare repository diff"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        record={'id':'round','workspace':str(workspace),'base_commit':base,'commit':head,
                'repository':str(bare),'git_dir':str(bare)}
        with patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv):
            result=model('validate',{'product':product,'record':record,'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        root=Path(result['evidence'])
        facts=json.loads((root/'verification-facts.json').read_text())
        self.assertEqual(facts['source'],'repository')
        self.assertEqual((facts['base_commit'],facts['commit'],facts['merge_base']),(base,head,base))
        self.assertEqual([line.split('\t')[-1] for line in facts['changed']],['app.py'])
        self.assertIn('+change',(root/'verification-diff.patch').read_text())

    def test_delivery_verify_embeds_self_contained_diff_evidence(self):
        """交付复验把差异事实落盘到证据目录，并写入独立验证检查与 verification.json。"""
        import hashlib as _hashlib
        from autopilot.delivery_review import verify
        (self.repo/'app.py').write_text('base\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','base app')
        base=git(self.repo,'rev-parse','HEAD')
        (self.repo/'app.py').write_text('base\nchange\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','change the app')
        head=git(self.repo,'rev-parse','HEAD')
        adapter=self.root/'fake-verify-adapter'
        adapter.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'payload=json.load(sys.stdin)\n'
            'material=payload["verification"]\n'
            'requirement=payload["requirement"]\n'
            'assert material["diff_file"] and material["facts_file"]\n'
            'assert "+change" in Path(material["diff_file"]).read_text()\n'
            'assert material["baseline_acceptance"] is True\n'
            'assert material["acceptance_checks"]==["generic-projects"]\n'
            'assert requirement["acceptance"] and "deadbeef" in requirement["acceptance"][0]\n'
            'assert requirement["resolution_probes"]==[{"path":"check:generic-projects",'
            '"pointer":"/status","operator":"equals","expected":"pass"}]\n'
            'print(json.dumps({"status":"pass","checks":['
            '{"name":"install-0","status":"pass","required":True},'
            '{"name":"独立业务验证","status":"pass","required":True,"evidence":{"provider":"codex"}}]}))\n')
        adapter.chmod(0o700)
        product=self.p|{'adapter':[str(adapter)],'project_config':{'version':1,'commands':{},
            'acceptance_checks':{'generic-projects':[sys.executable,'-c','pass']}}}
        folder=self.root/'verification-evidence';folder.mkdir()
        result=verify({'product':product,'record':{'title':'fixture','baseline_source_digest':'deadbeef'},'requirements':[]},
                      self.repo,head,base,folder)
        self.assertEqual(result['status'],'pass',result)
        facts=json.loads((folder/'verification-facts.json').read_text())
        self.assertEqual(facts['merge_base'],base)
        check=next(c for c in result['checks'] if c['name']=='独立业务验证')
        self.assertEqual(check['evidence']['merge_base'],base)
        self.assertEqual(check['evidence']['diff_sha256'],
                         _hashlib.sha256((folder/'verification-diff.patch').read_bytes()).hexdigest())
        self.assertEqual(check['evidence']['changed_files'],['M\tapp.py'])
        written=json.loads((folder/'verification.json').read_text())
        self.assertEqual(written['verification']['merge_base'],base)
        self.assertTrue(written['verification']['baseline_acceptance'])
        self.assertEqual(written['verification']['acceptance_checks'],['generic-projects'])
        self.assertNotIn('diff',written['verification'])

    def test_delivery_verify_rejects_empty_acceptance_without_traceable_source(self):
        """无业务需求、无导入摘要、无已登记检查时必须 fail-fast，不伪造通过。"""
        from autopilot.delivery_review import verify
        (self.repo/'app.py').write_text('base\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','base app')
        base=git(self.repo,'rev-parse','HEAD')
        (self.repo/'app.py').write_text('base\nchange\n')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','change the app')
        head=git(self.repo,'rev-parse','HEAD')
        marker=self.root/'verify-ran-without-acceptance'
        adapter=self.root/'fake-verify-adapter-empty'
        adapter.write_text('#!'+sys.executable+'\nfrom pathlib import Path\n'
            'Path('+repr(str(marker))+').write_text("ran")\nprint("{}")\n')
        adapter.chmod(0o700)
        product=self.p|{'adapter':[str(adapter)],'project_config':{'version':1,'commands':{},'acceptance_checks':{}}}
        folder=self.root/'verification-empty';folder.mkdir()
        result=verify({'product':product,'record':{'title':'fixture'},'requirements':[]},
                      self.repo,head,base,folder)
        self.assertEqual(result['status'],'blocked',result)
        self.assertFalse(marker.exists())
        written=json.loads((folder/'verification.json').read_text())
        self.assertEqual(written['status'],'blocked')

    def test_delivery_verify_fails_fast_when_diff_evidence_missing(self):
        """差异证据无法生成时不得在无证据情况下启动验证模型。"""
        from autopilot.delivery_review import verify
        marker=self.root/'verify-ran-without-diff'
        adapter=self.root/'fake-verify-adapter-nodiff'
        adapter.write_text('#!'+sys.executable+'\nfrom pathlib import Path\n'
            'Path('+repr(str(marker))+').write_text("ran")\nprint("{}")\n')
        adapter.chmod(0o700)
        product=self.p|{'adapter':[str(adapter)],'project_config':{'version':1,'commands':{},
            'acceptance_checks':{'generic-projects':[sys.executable,'-c','pass']}}}
        folder=self.root/'verification-nodiff';folder.mkdir()
        result=verify({'product':product,'record':{'title':'fixture','baseline_source_digest':'deadbeef'},'requirements':[]},
                      self.repo/'missing','a'*40,'b'*40,folder)
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn('独立验证缺少自包含差异证据',result['reason'])
        self.assertFalse(marker.exists())
        written=json.loads((folder/'verification.json').read_text())
        self.assertEqual(written['verification']['status'],'blocked')

    def test_validate_prompt_includes_truncated_diff_summary(self):
        """超大差异被截断时，提示词仍给出 merge_base/changed_files/diff_sha256 与落盘指引。"""
        from autopilot import codex_executor
        from autopilot.codex_executor import execute as model
        workspace,bare,base,head=self.worktree_fixture()
        fake=self.root/'codex-large-diff'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'assert "diff_sha256=" in prompt\n'
            'assert "merge_base=" in prompt\n'
            'assert "改动文件 1 个" in prompt\n'
            'assert "verification-diff.patch" in prompt\n'
            'Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","summary":"large diff summary"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        with patch.object(codex_executor,'INLINE_DIFF_LIMIT',8), patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv):
            result=model('validate',{'product':product,'record':{'workspace':str(workspace),'base_commit':base,'commit':head},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)


    def test_validate_generates_diff_when_adapter_spec_is_missing(self):
        """缺失 adapter_spec 的产品也必须拿到 base..commit 差异，不再静默跳过。"""
        from autopilot.codex_executor import execute as model
        workspace,bare,base,head=self.worktree_fixture()
        fake=self.root/'codex-missing-spec'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'assert "verification-diff.patch" in prompt\n'
            'assert "+change" in prompt\n'
            'Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","summary":"generated without adapter kind"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}}}
        self.assertFalse(product.get('adapter_spec'))
        result=model('validate',{'product':product,'record':{'id':'round','workspace':str(workspace),'base_commit':base,'commit':head},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        root=Path(result['evidence'])
        facts=json.loads((root/'verification-facts.json').read_text())
        self.assertEqual((facts['base_commit'],facts['commit'],facts['merge_base']),(base,head,base))
        self.assertEqual([line.split('\t')[-1] for line in facts['changed']],['app.py'])
        self.assertIn('+change',(root/'verification-diff.patch').read_text())

    def test_validate_generates_diff_for_legacy_adapter_spec(self):
        """显式 legacy kind 同样生成差异证据，验证者只读内联差异即可判断。"""
        from autopilot.codex_executor import execute as model
        workspace,bare,base,head=self.worktree_fixture()
        fake=self.root/'codex-legacy-spec'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'assert "verification-diff.patch" in prompt and "+change" in prompt\n'
            'Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","summary":"legacy adapter diff"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}},
                        'adapter_spec':{'version':1,'kind':'legacy','capabilities':['verify']}}
        result=model('validate',{'product':product,'record':{'id':'round','workspace':str(workspace),'base_commit':base,'commit':head},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        self.assertIn('+change',(Path(result['evidence'])/'verification-diff.patch').read_text())

    def test_generic_verify_prepares_diff_before_starting_verifier(self):
        """command 适配器 verify 先落盘差异并透传，独立验证不再依赖 git 元数据可读。"""
        from autopilot.generic_adapter import execute as generic_execute
        workspace,bare,base,head=self.worktree_fixture()
        fake=self.root/'codex-adapter-verify'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'assert "verification-diff.patch" in prompt\n'
            'assert "+change" in prompt\n'
            'Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","reason":"","summary":"verified via controller diff"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(workspace),'agents':{'verification':{'provider':'codex','model':'fixture','bin':str(fake)}},
                        'adapter_spec':{'version':1,'kind':'legacy','capabilities':['verify']},
                        'project_config':{'version':1,'commands':{},'web':False}}
        record={'id':'verify-round','workspace':str(workspace),'base_commit':base,'commit':head}
        onboarding={'status':'pass','checks':[{'name':'build-0','status':'pass','required':True}]}
        with patch('autopilot.generic_adapter.verify', return_value=onboarding) as simulate:
            result=generic_execute('verify',{'product':product,'record':record,'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        simulate.assert_called_once()
        check=next(c for c in result['checks'] if c['name']=='独立业务验证')
        self.assertEqual(check['status'],'pass')
        root=self.store.state/'autopilot/candidates'/product['id']/record['id']
        facts=json.loads((root/'verification-facts.json').read_text())
        self.assertEqual((facts['base_commit'],facts['commit'],facts['merge_base']),(base,head,base))
        self.assertIn('+change',(root/'verification-diff.patch').read_text())

    def test_codex_state_is_private_and_login_reference_is_readonly(self):
        from autopilot.codex_executor import execute as model
        login=self.root/'fixture-codex';login.mkdir()
        (login/'auth.json').write_text('synthetic fixture; not a credential')
        fake=self.root/'codex-state-probe'
        fake.write_text('#!'+sys.executable+'\nimport os,sys,json\nfrom pathlib import Path\np=Path(os.environ["CODEX_HOME"])\nassert p.name=="codex-state"\nassert (p/"auth.json").is_symlink()\n(p/"private-state").write_text("isolated")\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","plan":"private state"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(self.repo),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        with patch.dict(os.environ, {'CODEX_HOME':str(login)}), patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv) as sandbox:
            result=model('plan',{'product':product,'record':{},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass')
        self.assertIn(login/'auth.json', sandbox.call_args.kwargs['readonly_roots'])
        self.assertFalse((login/'private-state').exists())

    def test_readonly_model_cannot_write_workspace_inside_allowed_temp(self):
        if sys.platform!='darwin' or os.environ.get('DSH_PROJECT_ISOLATED'):
            self.skipTest('实际只读权限由外层主机验证，macOS 不允许嵌套 Seatbelt')
        from autopilot.codex_executor import execute as model
        fake=self.root/'codex-readonly-probe'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\ntry:\n Path("unexpected-write").write_text("must be denied")\n raise RuntimeError("readonly workspace escaped through its temporary ancestor")\nexcept PermissionError: pass\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","plan":"write denied"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(self.repo),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        result=model('plan',{'product':product,'record':{},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        self.assertFalse((self.repo/'unexpected-write').exists())

    def test_generic_model_cannot_read_other_home_directories(self):
        if sys.platform!='darwin' or os.environ.get('DSH_PROJECT_ISOLATED'):
            self.skipTest('实际用户目录权限由外层主机验证')
        from autopilot.codex_executor import execute as model
        with tempfile.TemporaryDirectory(prefix='.dsh-boundary-test-',dir=Path.home()) as private:
            protected=Path(private)/'fixture.txt';protected.write_text('test fixture; no real credentials')
            fake=self.root/'codex-private-probe'
            fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\ntry:\n Path('+repr(str(protected))+').read_text()\n raise RuntimeError("another home directory was readable")\nexcept PermissionError: pass\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","plan":"private read denied"}))\n')
            fake.chmod(0o700)
            product=self.p|{'repository':str(self.repo),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
            result=model('plan',{'product':product,'record':{},'state_root':str(self.store.state/'autopilot')})
            self.assertEqual(result['status'],'pass',result)

    def test_python_project_scan_is_recorded_and_source_remains_unchanged(self):
        if sys.platform!='darwin' or os.environ.get('DSH_PROJECT_ISOLATED'):
            self.skipTest('隔离启动由外层验证运行')
        config={'version':1,'commands':{'test':[[sys.executable,'-c','assert 2+2==4']],
                'start':[[sys.executable,'-m','http.server','{port}','--bind','127.0.0.1']]},'web':False,'blockers':[]}
        (self.repo/'pyproject.toml').write_text('[project]\nname="fixture"\nversion="1.0.0"\n')
        (self.repo/'.autopilot.json').write_text(json.dumps(config))
        before=git(self.repo,'status','--porcelain')
        scan=self.control.mutate('/scans',{'source':str(self.repo)})
        result=execute('scan',{'record':scan,'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(before,git(self.repo,'status','--porcelain'))
        self.assertEqual(result['business_acceptance'],'pending')

    def test_migration_preserves_ids_evidence_and_is_idempotent(self):
        old=self.ledger.create('runs',{'product_id':self.p['id'],'receipts':[{'evidence':'original'}]},'cancelled')
        migrate(self.ledger);first=self.ledger.get('products',self.p['id'])
        migrate(self.ledger)
        self.assertEqual(first,self.ledger.get('products',self.p['id']))
        self.assertEqual(old,self.ledger.get('runs',old['id']))

    def test_retired_project_has_no_background_dispatch(self):
        self.p=self.ledger.update('products',self.p['id'],self.p['version'],{'automation_disabled':True,'daily_report_enabled':True,'daily_report_enabled_at':1},'active')
        self.ledger.create('requirements',{'product_id':self.p['id'],'classification':'development','in_scope':True},'pending')
        scheduler=Scheduler(self.store)
        with patch.object(scheduler,'start_call') as start:
            scheduler.tick();start.assert_not_called()
        self.assertEqual(self.ledger.list('daily_reports'),[])
        self.assertEqual(self.ledger.list('runs'),[])

    def test_drain_reconciles_unknown_launch_without_relaunch(self):
        import time
        scheduler=Scheduler(self.store)
        directory=scheduler.root/'calls'/'interrupted';directory.mkdir(parents=True)
        request=directory/'request.json';request.write_text('{}')
        self.ledger.create('runs',{'product_id':self.p['id'],'call':{'path':str(request),'started':time.time()-60}},'planning')
        self.assertTrue(updater.busy(self.store.state))
        (scheduler.root/'update-drain.json').write_text('{}')
        with patch.object(scheduler,'start_call') as launch:
            scheduler.tick();launch.assert_not_called()
        self.assertTrue(json.loads((directory/'result.json').read_text())['uncertain'])
        self.assertFalse(updater.busy(self.store.state))

    def test_generic_probe_allowlist_rejects_unknown_and_encoded_paths(self):
        product={'project_config':{'readonly_paths':['/api/status']}}
        validate(['/api/status'],product)
        for path in ['/api/admin','https://example.com','//evil','/api/%2e%2e','/api/status?write=1']:
            with self.assertRaises(ValueError):validate([path],product)

    def test_snapshot_excludes_info_and_old_evidence(self):
        (self.repo/'info').mkdir();(self.repo/'info/private.txt').write_text('private')
        (self.repo/'old.patch').write_text('old')
        snapshot(self.repo,self.root/'snapshot',exclude=['info','old.patch'])
        self.assertFalse((self.root/'snapshot/info').exists());self.assertFalse((self.root/'snapshot/old.patch').exists())
        self.assertTrue((self.repo/'info/private.txt').exists())

    def test_fresh_environment_does_not_inherit_credentials(self):
        with patch.dict('os.environ',{'AWS_SECRET_ACCESS_KEY':'secret','DSH_HOME':'/private/user'}):
            env=environment(self.root/'execution')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',env)
        self.assertTrue(env['DSH_HOME'].startswith(str(self.root)))

    @unittest.skipUnless(sys.platform=='darwin' and not os.environ.get('DSH_PROJECT_ISOLATED'), '实际 Seatbelt 边界测试由外层验证运行，macOS 不允许嵌套沙箱')
    def test_real_isolated_python_start_and_stop(self):
        config={'commands':{'start':[[sys.executable,'-m','http.server','{port}','--bind','127.0.0.1']]},'web':False,'startup_timeout':5}
        result=startup(self.repo,self.root/'execution',config)
        self.assertEqual(result['status'],'pass',result)
        self.assertTrue(result['stopped'])

    @unittest.skipUnless(sys.platform=='darwin' and not os.environ.get('DSH_PROJECT_ISOLATED'), '实际 Seatbelt 边界测试由外层验证运行，macOS 不允许嵌套沙箱')
    def test_sandbox_denies_private_reads_and_production_ports(self):
        secret=self.root/'private.txt';secret.write_text('private')
        script='from pathlib import Path;Path('+repr(str(secret))+').read_text()'
        result=run([sys.executable,'-c',script],self.repo,self.root/'execution','read')
        self.assertEqual(result['status'],'fail')
        script='import socket;socket.create_connection(("127.0.0.1",13084),timeout=1)'
        result=run([sys.executable,'-c',script],self.repo,self.root/'execution2','network',ports=['test-loopback'])
        self.assertEqual(result['status'],'fail')

    def test_generic_business_probe_requires_assertion(self):
        from autopilot.probes import check
        product={'project_config':{'readonly_paths':['/api/status']}}
        with self.assertRaises(ValueError):check(['/api/status'],lambda _: {'ok':False},product)
        assertion={'path':'/api/status','pointer':'/ready','operator':'equals','expected':True}
        self.assertFalse(check([assertion],lambda _: {'ready':False},product))

    def test_pending_self_update_keeps_release_and_run(self):
        run=self.ledger.create('runs',{'product_id':self.p['id'],'release_id':'release'},'deploying')
        result=Scheduler(self.store).complete_action(run,self.p,'publish',{'status':'deferred','update':'job.json','reason':'waiting'})
        self.assertEqual(result['status'],'deploying');self.assertEqual(result['release_id'],'release')
        self.assertEqual(result['update'],'job.json')

    def test_updater_waits_then_switches_and_completes(self):
        config,path=self.update_fixture()
        with patch.object(updater,'busy',return_value=True),patch.object(updater,'stop_services') as stopped:
            updater.step(config,path);stopped.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['status'],'draining')
        with patch.object(updater,'busy',return_value=False),patch.object(updater,'stop_services'),patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=True):
            updater.step(config,path);updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'completed')
        self.assertFalse((self.store.state/'autopilot/update-drain.json').exists())
        with patch.object(updater,'restart') as restart:
            updater.step(config,path);restart.assert_not_called()

    def test_updater_rolls_back_without_replacing_database(self):
        config,path=self.update_fixture()
        with patch.object(updater,'busy',return_value=False),patch.object(updater,'stop_services'),patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=True):
            updater.step(config,path)
        self.ledger.create('signals',{'product_id':self.p['id'],'summary':'new history'})
        with patch.object(updater,'stop_services'),patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=False):
            updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rollback_check')
        with patch.object(updater,'healthy',return_value=True):updater.reconcile_rollback(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rolled_back')
        self.assertEqual(len(self.ledger.list('signals')),1)
        self.assertEqual(Path(config['current']).resolve(),self.root/'old')

    def test_updater_rejects_tampering_before_stopping(self):
        config,path=self.update_fixture()
        manifest=json.loads(Path(json.loads(path.read_text())['manifest']).read_text())
        (Path(manifest['artifact'])/'bad').write_text('tampered')
        with patch.object(updater,'stop_services') as stop_service:
            updater.step(config,path);stop_service.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['status'],'blocked')

    def test_updater_retries_failed_rollback_and_cleans_completed_drain(self):
        config,path=self.update_fixture()
        with patch.object(updater,'busy',return_value=False),patch.object(updater,'stop_services'),patch.object(updater,'restart',side_effect=OSError('temporarily unavailable')):
            updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rollback_retry')
        job=json.loads(path.read_text());path.write_text(json.dumps(job|{'retry_at':0}))
        with patch.object(updater,'stop_services'),patch.object(updater,'restart'):
            updater.reconcile_rollback(config,path)
        with patch.object(updater,'healthy',return_value=True):updater.reconcile_rollback(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rolled_back')
        drain=self.store.state/'autopilot/update-drain.json'
        drain.write_text(json.dumps({'job':str(path)}))
        updater.step(config,path)
        self.assertFalse(drain.exists())

    def test_updater_recovers_switch_intent_after_interruption(self):
        config,path=self.update_fixture()
        job=json.loads(path.read_text())|{'status':'switching','previous':str(self.root/'old')}
        path.write_text(json.dumps(job))
        with patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=True):
            updater.step(config,path);updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'completed')

    def update_fixture(self):
        root=self.store.state/'autopilot/candidates/fixture';artifact=root/'artifact';artifact.mkdir(parents=True)
        marker={'product_id':self.p['id'],'commit':'new','release_id':'new'}
        (artifact/'autopilot-release.json').write_text(json.dumps(marker))
        manifest=root/'manifest.json';manifest.write_text(json.dumps(marker|{'artifact':str(artifact),'artifact_hash':manifest_hash(artifact),
            'database_compatibility':'backward-compatible','checks':[{'status':'pass','required':True}]}))
        old=self.root/'old';old.mkdir();(old/'autopilot-release.json').write_text(json.dumps(marker|{'commit':'old'}))
        current=self.root/'current';current.symlink_to(old,target_is_directory=True)
        path=self.store.state/'autopilot/updates/job.json';path.parent.mkdir();path.write_text(json.dumps({'status':'queued','manifest':str(manifest),'product_id':self.p['id'],'expected_commit':'new'}))
        return {'state':str(self.store.state),'current':str(current),'observation_seconds':0,'restart_commands':[],'stop_commands':[]},path


if __name__=='__main__':unittest.main()
