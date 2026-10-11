import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Drawer, Space, Tag } from 'antd';
import { api } from './api';
import { errorText } from './errors';

type Job = {status:string; reason?:string; master_commit?:string;
  branches:{branch:string;status:string;reason?:string;workspace?:string}[];
  events:{at:number;kind:string;text:string;detail?:unknown}[]};
const active = (status?:string) => ['queued','running','planning','resolving'].includes(status || '');
const labels:Record<string,string> = {idle:'尚未同步',queued:'等待执行',running:'同步中',planning:'分析冲突方案',resolving:'处理冲突',blocked:'需要处理',completed:'同步完成',pass:'已同步'};

export default function MasterSyncTerminal({productId,enabled}:{productId:string;enabled:boolean}) {
  const [open,setOpen] = useState(false);
  const [starting,setStarting] = useState(false);
  const [error,setError] = useState('');
  const path = `/products/${productId}/sync-master`;
  const query = useQuery<Job>({queryKey:['master-sync',productId], queryFn:()=>api(path), enabled:open, refetchInterval:1500, retry:false});
  const start = async () => {
    setOpen(true); setStarting(true); setError('');
    try { await api(path,{}); await query.refetch(); }
    catch(e) { setError(errorText(e)); }
    finally { setStarting(false); }
  };
  const job = query.data;
  return <>
    <Button disabled={!enabled} onClick={()=>void start()}>同步 master</Button>
    <Drawer title="同步 master · 执行终端" width={880} open={open} onClose={()=>setOpen(false)}>
      <Space direction="vertical" style={{width:'100%'}} size="middle">
        <Alert type="info" showIcon message="最新 master → feat / release" description="在本机逐个同步分支。遇到冲突先记录方案，再按方案处理。正在执行任务或有未提交改动的分支会保留原状并说明原因。关闭终端不影响执行。"/>
        {(error || query.error) && <Alert type="error" showIcon message={error || errorText(query.error)}/>}
        <Space><Tag color={job?.status==='blocked'?'warning':job?.status==='completed'?'success':'processing'}>{starting?'提交中':labels[job?.status || 'idle'] || job?.status}</Tag>
          <Button onClick={()=>void query.refetch()}>刷新终端</Button>
          <Button disabled={starting || active(job?.status)} onClick={()=>void start()}>重新同步</Button>
        </Space>
        <div role="status">{job?.reason}</div>
        {job?.master_commit && <div>本次 master：<code>{job.master_commit.slice(0,12)}</code></div>}
        {job?.branches.map(row=><div key={row.branch}><Tag color={row.status==='blocked'?'warning':'default'}>{labels[row.status] || row.status}</Tag><code>{row.branch}</code> · {row.reason || '执行中'}{row.workspace && <div>保留的冲突工作区：<code>{row.workspace}</code></div>}</div>)}
        <div role="log" aria-label="分支同步执行日志" style={{background:'#10151e',color:'#d9e2ef',padding:16,borderRadius:8,minHeight:220,maxHeight:'55vh',overflow:'auto'}}>
          {!job?.events.length && <span>等待执行日志…</span>}
          {job?.events.map((event,index)=><div key={index} style={{marginBottom:12,color:event.kind==='error'?'#ff9c9c':event.kind==='plan'?'#9bdbbf':undefined}}>
            <small>{new Date(event.at*1000).toLocaleTimeString()}</small>
            <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',margin:'4px 0'}}>{event.text}{event.detail ? '\n'+(typeof event.detail==='string'?event.detail:JSON.stringify(event.detail,null,2)):''}</pre>
          </div>)}
        </div>
      </Space>
    </Drawer>
  </>;
}
