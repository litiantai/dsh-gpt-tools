"""角色工作流的配置、真实命令证据、失效边界和兼容回归。"""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot import quality, role_skills, role_workflows, jvm
from autopilot.workspace import git
from autopilot.local_testing import environment, run


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@localhost')
        (self.repo/'app.txt').write_text('original')
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', 'initial')
        self.config = {'commands': {'test': [[sys.executable, '-c', 'print("real check")']]}}

    def tearDown(self):
        role_skills.READS.set(())
        self.temp.cleanup()

    def runner(self, argv, cwd, folder, name, phase):
        return run(argv, cwd, folder, name, env=environment(folder), timeout=5)

    def check(self, config=None):
        config = config or self.config
        session = quality.prepare(self.repo, config, self.root/'evidence')
        quality.execute(session, quality.PHASES, self.runner)
        return session, quality.finish(session)

    def test_all_roles_and_shared_profiles(self):
        for role in ('discovery','implementation','verification','code-review','acceptance'):
            flow = role_workflows.load(role)
            self.assertTrue(flow['steps'])
            if role != 'discovery':
                self.assertIn('java', flow['profiles'])
                self.assertIn('dsh-role-verification/references/java-quality.md', flow['resources'])
        self.assertIsNone(role_workflows.load('research'))

    def test_malformed_yaml_versions_aliases_and_duplicate_keys_block(self):
        for text in ('version: 2', 'version: 1\nversion: 1', 'version: 1\na: &a [1]\nb: *a', 'version: 1\na: !exec test'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                role_workflows.parse(text)

    def test_invalid_reference_does_not_fall_back(self):
        skills = self.root/'skills'
        shutil.copytree(ROOT/'skills', skills)
        file = skills/'dsh-role-discovery/workflow.yaml'
        file.write_text(file.read_text()+'profiles:\n  java: ../../escape.yaml\n')
        with patch.object(role_skills, 'ROOT', skills), self.assertRaises(ValueError):
            role_workflows.load('discovery')

    def test_snapshot_resumes_original_workflow_and_cross_role_rules(self):
        skills = self.root/'skills'; shutil.copytree(ROOT/'skills', skills)
        with patch.object(role_skills, 'ROOT', skills):
            prompt, manifest = role_skills.bind('develop', self.root/'call', {}, 'implement', {}, config=self.config, workspace=self.repo)
            saved = self.root/'call/role-skill/dsh-role-verification/references/java-quality.md'
            self.assertTrue(saved.exists())
            (skills/'dsh-role-implementation/workflow.yaml').write_text('version: 99')
            resumed, again = role_skills.bind('develop', self.root/'call', {}, 'implement', {}, config=self.config, workspace=self.repo)
            self.assertEqual(prompt, resumed); self.assertEqual(manifest, again)
            with self.assertRaises(ValueError):
                role_skills.bind('develop', self.root/'new-call', {}, 'implement', {})
            saved.write_text('tampered')
            with self.assertRaises(ValueError):
                role_skills.bind('develop', self.root/'call', {}, 'implement', {}, config=self.config, workspace=self.repo)

    def test_real_receipt_reusable_but_model_claim_is_not(self):
        _, proof = self.check()
        self.assertEqual(proof['status'], 'pass')
        self.assertEqual(quality.validate(proof, self.repo, self.config, trusted_root=self.root)['status'], 'pass')
        self.assertEqual(quality.validate(proof, self.repo, self.config, trusted_root=self.root)['status'], 'pass')
        with self.assertRaises(ValueError):
            quality.validate({'status': 'pass', 'checks': [{'status': 'pass'}]}, self.repo, self.config)
        with self.assertRaises(ValueError):
            quality.validate(proof, self.repo, self.config, trusted_root=self.repo)

    def test_pre_workflow_snapshot_resumes_after_upgrade(self):
        skills = self.root/'old-skills'; role = skills/'dsh-role-implementation';role.mkdir(parents=True)
        (skills/'roles.json').write_text('{}')
        (role/'SKILL.md').write_text('---\nname: dsh-role-implementation\n---\noriginal')
        with patch.object(role_skills, 'ROOT', skills):
            prompt, manifest = role_skills.bind('develop', self.root/'old-call', {}, 'original instruction', {})
            manifest.pop('binding_hash')
            (self.root/'old-call/role-skill/manifest.json').write_text(json.dumps(manifest))
            (role/'workflow.yaml').write_text('version: 99')
            resumed, _ = role_skills.bind('develop', self.root/'old-call', {}, 'original instruction', {})
            self.assertEqual(prompt, resumed)

    def test_source_config_log_and_receipt_changes_invalidate(self):
        _, proof = self.check()
        (self.repo/'app.txt').write_text('modified')
        with self.assertRaises(ValueError): quality.validate(proof, self.repo, self.config)
        (self.repo/'app.txt').write_text('original')
        with self.assertRaises(ValueError): quality.validate(proof, self.repo, self.config | {'stack':'python'})
        log = Path(proof['checks'][0]['log']); original=log.read_text();log.write_text('tampered')
        with self.assertRaises(ValueError): quality.validate(proof, self.repo, self.config)
        log.write_text(original)
        Path(proof['receipt']).write_text('{}')
        with self.assertRaises(ValueError): quality.validate(proof, self.repo, self.config)

    def test_source_change_during_check_is_blocked(self):
        config = {'commands': {'test': [[sys.executable, '-c', "from pathlib import Path; Path('app.txt').write_text('changed')"]]}}
        _, proof = self.check(config)
        self.assertEqual(proof['status'], 'blocked')
        self.assertIn('源码', proof['reason'])

    def test_java_compilation_failure_stops_tests(self):
        config = {'stack':'java', 'commands': {'compile': [[sys.executable, '-c', 'raise SystemExit(2)']],
                  'test': [[sys.executable, '-c', "raise Exception('must not execute')"]]}}
        _, proof = self.check(config)
        self.assertEqual(proof['status'], 'fail')
        self.assertEqual([c['phase'] for c in proof['checks']], ['compile'])
        self.assertEqual(proof['checks'][0]['exit_code'], 2)

    def test_java_missing_compile_blocks_before_any_commands(self):
        with self.assertRaisesRegex(ValueError, 'compile'):
            quality.prepare(self.repo, self.config | {'stack':'java'}, self.root/'evidence')

    def test_mixed_modules_compile_before_frontend_and_java_tests(self):
        for folder in ('backend', 'frontend'):
            (self.repo/folder).mkdir()
        cmd = [sys.executable, '-c', 'print("module check")']
        config = {'modules': [
            {'id':'backend','path':'backend','stack':'java','commands':{'compile':[cmd],'test':[cmd]}},
            {'id':'frontend','path':'frontend','stack':'vue','commands':{'typecheck':[cmd],'build':[cmd],'test':[cmd]}}]}
        _, proof = self.check(config)
        self.assertEqual(proof['status'], 'pass')
        self.assertEqual([c['phase'] for c in proof['checks']], ['compile','typecheck','build','test','test'])
        self.assertEqual([c['module'] for c in proof['checks']], ['backend','frontend','frontend','backend','frontend'])

    def test_react_vue_detection_and_explicit_override(self):
        for stack in ('react','vue'):
            (self.repo/'package.json').write_text(json.dumps({'dependencies':{stack:'1.0.0'}}))
            self.assertEqual(role_workflows.modules(self.repo, {})[0]['stack'], stack)
            self.assertEqual(role_workflows.modules(self.repo, {'stack':'generic'})[0]['stack'], 'generic')

    def test_module_escape_duplicate_and_unknown_stack_block(self):
        for item in ({'id':'bad','path':'../outside'}, {'id':'root','path':'.'}, {'id':'bad','path':'.','stack':'typo'}):
            with self.assertRaises(ValueError):
                role_workflows.modules(self.repo, {'commands':self.config['commands'],'modules':[item]})

    def test_missing_exit_code_or_log_never_passes(self):
        session = quality.prepare(self.repo, self.config, self.root/'evidence')
        quality.execute(session, quality.PHASES, lambda *args: {'status':'pass','exit_code':0})
        self.assertEqual(quality.finish(session)['status'], 'blocked')

    def test_missing_steps_and_incomplete_review_proof_block_acceptance(self):
        session = quality.prepare(self.repo, self.config, self.root/'evidence')
        self.assertEqual(quality.finish(session)['status'], 'blocked')
        session = quality.prepare(self.repo, self.config, self.root/'review', role='code-review', action='review', phases=quality.PHASES[:-1])
        quality.execute(session, quality.PHASES[:-1], self.runner)
        proof = quality.finish(session)
        with self.assertRaisesRegex(ValueError, '全部检查'):
            quality.validate(proof, self.repo, self.config)

    def test_watch_does_not_run_quality_commands(self):
        from autopilot.generic_adapter import execute
        product = {'id':'watch','adapter_spec':{'capabilities':['inspect']}, 'project_config':self.config}
        with patch('autopilot.generic_adapter.probe', return_value={'status':'blocked','reason':'offline'}), patch('autopilot.quality.execute') as spawn:
            result = execute('inspect', {'product':product,'record':{},'state_root':str(self.root/'state/autopilot')})
        spawn.assert_not_called()
        self.assertEqual(result['workflow']['role'], 'discovery')
        self.assertEqual(result['workflow']['steps'][-1]['status'], 'pending')

    def test_jvm_dependency_policy_and_offline_commands(self):
        self.assertTrue(jvm.install_allowed(['mvn','dependency:go-offline'], 'jvm-resolve'))
        self.assertTrue(jvm.install_allowed(['gradle','autopilotResolveDependencies'], 'jvm-resolve'))
        self.assertFalse(jvm.install_allowed(['mvn','test'], 'jvm-resolve'))
        self.assertFalse(jvm.install_allowed(['gradle','dependencies'], 'jvm-resolve'))
        with patch('autopilot.jvm.java_home', return_value='/jdk'), patch('autopilot.jvm.shutil.which', return_value='/bin/mvn'):
            cmd = jvm.prepare(['mvn','test'], self.root, self.repo)
            self.assertIn('-o', cmd)
            self.assertTrue(any(s.startswith('-Dmaven.repo.local=') for s in cmd))
            with self.assertRaises(ValueError):
                jvm.prepare(['mvn','-Dmaven.repo.local=/tmp/shared','test'], self.root, self.repo)
        self.assertEqual(jvm.classify({'status':'fail','log_tail':'artifact has not been downloaded before'})['status'], 'blocked')
        self.assertEqual(jvm.classify({'status':'fail','log_tail':'COMPILATION ERROR'})['status'], 'fail')
        wrong_jdk = jvm.classify({'status':'fail','log_tail':'error: release version 25 not supported'})
        self.assertEqual(wrong_jdk['status'], 'blocked')
        self.assertIn('安装', wrong_jdk['install_hint'])

    def test_missing_local_runtime_provides_install_hint_without_spawning(self):
        config = {'commands': {'compile': [['missing-workflow-runtime', 'compile']]}}
        session = quality.prepare(self.repo, config, self.root/'missing')
        with patch('autopilot.quality.shutil.which', return_value=None), patch.object(self, 'runner') as spawn:
            quality.execute(session, quality.PHASES, spawn)
        spawn.assert_not_called()
        proof = quality.finish(session)
        self.assertEqual(proof['status'], 'blocked')
        self.assertEqual(proof['checks'][0]['missing_tools'], ['missing-workflow-runtime'])
        self.assertIn('安装', proof['checks'][0]['install_hint'])

    def test_wrapper_never_downloads_a_missing_build_runtime(self):
        with patch('autopilot.jvm.java_home', return_value='/jdk'), patch('autopilot.jvm.subprocess.run') as spawn:
            for command in ('./mvnw', './gradlew'):
                with self.assertRaisesRegex(jvm.MissingRuntime, '不通过 wrapper 自动下载'):
                    jvm.prepare([command, 'test'], self.root, self.repo)
        spawn.assert_not_called()

    def test_code_review_never_calls_model_after_failed_compile(self):
        from autopilot.delivery_review import execute
        from unittest.mock import Mock
        head = git(self.repo, 'rev-parse', 'HEAD')
        record = {'head_sha':head,'base_sha':head,'pr_url':'https://github.com/example/repo/pull/1'}
        config = {'stack':'java','commands':{'compile':[[sys.executable,'-c','raise SystemExit(9)']],
                                           'test':[[sys.executable,'-c','print("must not run")']]}}
        request = {'product':{'project_config':config,'test_execution':'local'},'record':record}
        pr = {'state':'open','merged':False,'number':1,'head':{'sha':head},'base':{'sha':head}}
        with patch('autopilot.delivery_review.paths', return_value=(self.root,self.root/'batch',self.repo,self.repo)), \
                patch('autopilot.delivery_review.load_pull_request', return_value=(Mock(),pr)), \
                patch('autopilot.delivery_review.model') as model:
            result = execute('review', request)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['checks'][0]['exit_code'], 9)
        model.assert_not_called()

    def test_enrolled_acceptance_rejects_missing_quality_receipt(self):
        from types import SimpleNamespace
        from autopilot.scheduler import Scheduler
        scheduler = SimpleNamespace(root=self.root, block=lambda run,reason: {'status':'blocked','reason':reason})
        result = Scheduler.review(scheduler, {'status':'acceptance_review','workspace':str(self.repo)},
                                  {'project_config':{'workflow_version':1}}, {})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('质量回执', result['reason'])


if __name__ == '__main__':
    unittest.main()
