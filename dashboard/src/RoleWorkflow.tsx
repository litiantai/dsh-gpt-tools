import { Alert, Card, Space, Tag } from 'antd';

const roles: Record<string, string> = {
  discovery: 'Watch · 巡检与需求发现', implementation: '开发实施',
  verification: '独立验证', 'code-review': '代码质量评审', acceptance: '业务验收',
};
const phases: Record<string, string> = {install:'依赖准备', compile:'编译', typecheck:'类型检查', build:'构建', test:'测试', browser:'浏览器验收'};
const states: Record<string, string> = {pass:'通过', fail:'失败', blocked:'阻塞', pending:'待执行', reported:'已提交，待核对'};
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);

export default function RoleWorkflow({value}: {value: unknown}) {
  if (!object(value)) return null;
  const steps = Array.isArray(value.steps) ? value.steps.filter(object) : [];
  return <Card size="small" title={`角色工作流 · ${roles[String(value.role)] || String(value.role || '未记录角色')}`}>
    <Space wrap><Tag>版本 {String(value.version || 1)}</Tag><span>命令检查以控制器回执为准</span></Space>
    {typeof value.reason === 'string' && value.reason && <Alert type="warning" showIcon message={value.reason}/>}
    <ol aria-label="角色工作流步骤">{steps.map((step, index) => {
      const status = String(step.status || 'pending');
      const title = step.title || phases[String(step.phase)] || step.step_id || step.id;
      return <li key={String(step.step_id || step.id || index)} style={{marginBlock: 12}}>
        <Space wrap><strong>{String(title)}</strong>
          {step.module != null && <span>{String(step.module)} · {String(step.stack || 'generic')}</span>}
          <Tag color={status === 'pass' ? 'success' : ['fail','blocked'].includes(status) ? 'error' : 'default'}>{states[status] || status}</Tag>
        </Space>
        {typeof step.reason === 'string' && step.reason && <p>{step.reason}</p>}
      </li>;
    })}</ol>
  </Card>;
}
