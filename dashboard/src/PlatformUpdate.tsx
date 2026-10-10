import { Alert, Button, Space } from 'antd';
import { useAction, useData, time } from './components';

type Update = {
  active: boolean; job_id?: string; phase?: string; label?: string; commit?: string;
  reason?: string; impact?: string; remaining_seconds?: number; ends_at?: number;
  can_release?: boolean; release_requested?: boolean;
};

export default function PlatformUpdate() {
  const query = useData<Update>('/platform-update');
  const action = useAction();
  const update = query.data;
  if (query.error) return <Alert type="warning" showIcon message="暂时无法读取平台更新状态"
    action={<Button onClick={() => query.refetch()}>重新检查</Button>} />;
  if (!update?.active) return null;
  return <Alert role="status" type="warning" showIcon style={{ marginBottom: 16 }}
    message={update.label || '平台更新中'}
    description={<Space direction="vertical">
      <span>{update.impact}</span>
      {update.phase === 'observing' && <span>剩余约 {Math.ceil((update.remaining_seconds || 0) / 60)} 分钟，预计 {time(update.ends_at)} 恢复。</span>}
      {update.reason && <span>{update.reason}</span>}
      {update.release_requested ? <span>已请求手动解除，正在核对服务、数据库和调度心跳。</span>
        : update.phase === 'observing' && <span>可手动结束观察；健康检查通过后立即解除更新锁，保留原有任务状态。</span>}
    </Space>}
    action={<Button disabled={!update.can_release || action.isPending} loading={action.isPending}
      onClick={() => action.mutate({path: '/platform-update/release', body: {job_id: update.job_id, commit: update.commit}})}>
      {update.release_requested ? '正在核对健康' : '手动结束观察'}
    </Button>} />;
}
