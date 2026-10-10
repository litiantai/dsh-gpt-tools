"""使用已安装的 Harness 工具库验证回执注册、参数校验和落盘。"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get('DSH_AUTOPILOT_RUNTIME', Path.home()/'.dsh/supervisor/autopilot/runtime'))


@unittest.skipUnless((RUNTIME/'node_modules/@deepseek-ai/dsh-tools/lib/index.js').is_file(),
                     '需要已安装的 Harness 工具运行时')
class AutopilotGuardTests(unittest.TestCase):
    def test_real_tool_registers_validates_and_records_receipt(self):
        script = r"""
import assert from 'node:assert/strict';
import {readFileSync, statSync, existsSync} from 'node:fs';
import {apply} from './scripts/autopilot-guard.mjs';
let tool, guard;
apply({tools: {register: value => tool = value, guard: value => guard = value}});
assert.equal(tool.name, 'autopilot_result');
assert.equal(guard({name: 'autopilot_result', arguments: {}}), undefined);
assert.ok(guard({name: 'write', arguments: {}}));
const path = process.env.DSH_AUTOPILOT_RESULT;
await assert.rejects(tool.execute({status: 'pass', plan: ' '}), /通过回执必须提供/);
await assert.rejects(tool.execute({status: 'pass', plan: '方案',
  collaboration_requests: [{to_role: 42}]}));
assert.equal(existsSync(path), false);
const receipt = {status: 'pass', plan: '通知实现方案', collaboration_requests: [{
  to_role: 'verification', topic: '验收', content: '核对邮件回执', evidence_ids: ['test-evidence'],
}]};
assert.deepEqual(await tool.execute(receipt), {recorded: true});
assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), receipt);
assert.equal(statSync(path).mode & 0o777, 0o600);
assert.equal(existsSync(path + '.tmp'), false);
process.env.DSH_AUTOPILOT_PHASE = 'develop';
const implementation = {status: 'pass', summary: '已实现并验证'};
await tool.execute(implementation);
assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), implementation);
"""
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, DSH_AUTOPILOT_RUNTIME=str(RUNTIME),
                       DSH_AUTOPILOT_WORKER='guard-regression', DSH_AUTOPILOT_PHASE='plan',
                       DSH_AUTOPILOT_RESULT=str(Path(directory)/'result.json'))
            result = subprocess.run(['node', '--input-type=module', '-e', script],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
