"""本机会话声明与提交绑定，禁止外部认证路径及凭据输入。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.inspection_session import configuration, validate


class SessionTests(unittest.TestCase):
    def test_source_declaration_overrides_old_saved_project_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);spec={'path':'/api/bootstrap','probe_path':'/api/products'}
            (root/'.autopilot.json').write_text(json.dumps({'session_bootstrap':spec}))
            self.assertEqual(configuration(root,{'project_config':{}}),spec)

    def test_missing_declaration_does_not_assume_a_project_has_local_authentication(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(configuration(folder,{}))

    def test_invalid_external_paths_and_credentials_are_rejected(self):
        for value in ({'path':'//example.com','probe_path':'/api'}, {'path':'/\\remote','probe_path':'/api'},
                      {'path':'/api/bootstrap','probe_path':'/api','password':'secret'}, 'bootstrap'):
            with self.subTest(value=value),self.assertRaises(ValueError):validate(value)


if __name__=='__main__':unittest.main()
