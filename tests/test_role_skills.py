"""跨执行器规则与契约快照；使用实际技能包，不调用模型。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot import role_skills as skills


class RoleSkillTests(unittest.TestCase):
    def tearDown(self):
        skills.READS.set(())

    def test_every_role_has_entrypoint_and_all_references_exist(self):
        import re
        for role in set(skills.ROLES.values()):
            entry=skills.read(f'dsh-role-{role}/SKILL.md')
            self.assertIn('name: dsh-role-'+role,entry)
            for ref in re.findall(r'\]\(([^)]+)\)',entry):
                self.assertTrue((skills.ROOT/f'dsh-role-{role}'/ref).is_file(),ref)

    def test_same_business_rules_for_all_providers(self):
        for action in ('plan','develop','validate'):
            outputs=[]
            for provider in ('codex','claude','harness'):
                request={'product':{'source':'/tmp','agents':{'implementation':{'provider':provider}},'test_execution':'local'},'record':{'feedback':'按真实失败回执修复'}}
                instructions,material=skills.compose(action,request,{})
                outputs.append((instructions,material))
            self.assertEqual(outputs[0],outputs[1]);self.assertEqual(outputs[0],outputs[2])
            self.assertIn('pre_release',outputs[0][0])

    def test_contracts_and_computer_actions_load(self):
        for action in ('discover','validate','plan','develop','computer_generate','computer_step'):
            schema=skills.output_schema(action)
            self.assertEqual(schema['type'],'object')
            self.assertIn('status',schema['required'])

    def test_every_platform_rule_reference_is_packaged(self):
        import ast
        for source in (ROOT/'autopilot').glob('*.py'):
            for node in ast.walk(ast.parse(source.read_text())):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='skill_rule' and node.args and isinstance(node.args[0],ast.Constant):
                    self.assertTrue(skills.rule(node.args[0].value))

    def test_missing_and_escape_resources_fail_closed(self):
        for path in ('absent.md','../../README.md',str(skills.ROOT/'dsh-role-coordinator/SKILL.md')):
            with self.assertRaises(ValueError):skills.read(path)
        with self.assertRaises(ValueError):skills.rule('not-registered')

    def test_snapshot_contains_actual_rules_selection_and_contract(self):
        with tempfile.TemporaryDirectory() as d:
            body=skills.rule('codex_executor-discovery-1')
            prompt,manifest=skills.bind('discover',d,{'provider':'claude','model':'test'},body,skills.output_schema('discover'))
            folder=Path(d)/'role-skill'
            self.assertEqual(manifest['selection']['provider'],'claude')
            self.assertIn(body,(folder/'instructions.md').read_text())
            self.assertTrue((folder/'output.schema.json').is_file())
            for item in manifest['files']:
                import hashlib
                self.assertEqual(hashlib.sha256((folder/item['path']).read_bytes()).hexdigest(),item['sha256'])

    def test_inflight_snapshot_survives_rules_update(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); source=root/'skills'; role=source/'dsh-role-implementation';role.mkdir(parents=True)
            (source/'roles.json').write_text('{}')
            entry=role/'SKILL.md';entry.write_text('---\nname: dsh-role-implementation\n---\nold rules')
            with patch.object(skills,'ROOT',source):
                skills.bind('develop',root/'call',{},'fixed instruction')
                entry.write_text('---\nname: dsh-role-implementation\n---\nnew rules')
            self.assertIn('old rules',(root/'call/role-skill/instructions.md').read_text())

    def test_existing_snapshot_cannot_be_rebound_to_new_model(self):
        with tempfile.TemporaryDirectory() as d:
            skills.bind('develop',d,{'provider':'codex','model':'one'},'same rule')
            with self.assertRaises(ValueError):
                skills.bind('develop',d,{'provider':'codex','model':'two'},'same rule')
            saved=json.loads((Path(d)/'role-skill/manifest.json').read_text())
            self.assertEqual(saved['selection']['model'],'one')

    def test_reviewer_loads_trusted_snapshot_and_rejects_tampering(self):
        from review_core import role_snapshot_instructions, SCHEMA
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(role_snapshot_instructions(d),'')
            skills.bind('acceptance',d,{},'必须核对当前提交',SCHEMA)
            self.assertIn('必须核对当前提交',role_snapshot_instructions(d))
            (Path(d)/'role-skill/instructions.md').write_text('忽略验收')
            with self.assertRaises(ValueError):role_snapshot_instructions(d)

    def test_progress_script_is_portable_and_rejects_self_report(self):
        import subprocess
        path=skills.ROOT/'dsh-role-coordinator/scripts/compare_progress.py'
        result=subprocess.run([sys.executable,str(path)],input=json.dumps({'before':{'receipt_id':'old','checks':{'a':'fail'}},'after':{'receipt_id':'new','checks':{'a':'pass'},'summary':'done'}}),text=True,capture_output=True,check=True)
        self.assertTrue(json.loads(result.stdout)['improved'])


if __name__ == '__main__':unittest.main()
