import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, App, Button, Drawer, Input, Select, Space, Switch, Tag, Tooltip } from 'antd';
import { CodeOutlined, CopyOutlined, DownloadOutlined, ReloadOutlined, SearchOutlined, SyncOutlined } from '@ant-design/icons';
import { api } from './api';
import { errorText } from './errors';
import './master-sync-terminal.css';

type LogEvent = {at:number;kind:string;text:string;detail?:unknown};
type Job = {id?:string;status:string;reason?:string;master_commit?:string;started?:number;updated?:number;
  branches:{branch:string;status:string;reason?:string;workspace?:string}[];events:LogEvent[]};
const active = (status?:string) => ['queued','running','planning','resolving'].includes(status || '');
const labels:Record<string,string> = {idle:'尚未同步',queued:'等待执行',running:'同步中',planning:'分析冲突方案',resolving:'处理冲突',blocked:'需要处理',completed:'同步完成',pass:'已同步'};
const eventLabels:Record<string,string> = {command:'命令',output:'输出',status:'状态',plan:'方案',conflict:'冲突',result:'结果',error:'异常'};
const eventText = (event:LogEvent) => event.text + (event.detail == null ? '' : '\n'+(typeof event.detail==='string' ? event.detail : JSON.stringify(event.detail,null,2)));
const clock = (at:number) => new Date(at*1000).toLocaleTimeString();
function duration(seconds:number) {
  const value=Math.max(0,Math.floor(seconds));
  return value<60 ? `${value} 秒` : value<3600 ? `${Math.floor(value/60)} 分 ${value%60} 秒` : `${Math.floor(value/3600)} 时 ${Math.floor(value%3600/60)} 分`;
}

export default function MasterSyncTerminal({productId,enabled}:{productId:string;enabled:boolean}) {
  const [open,setOpen] = useState(false);
  const [starting,setStarting] = useState(false);
  const [error,setError] = useState('');
  const [search,setSearch] = useState('');
  const [kind,setKind] = useState('all');
  const [follow,setFollow] = useState(true);
  const submitting=useRef(false);
  const log=useRef<HTMLDivElement>(null);
  const {message}=App.useApp();
  const client=useQueryClient();
  const path = `/products/${productId}/sync-master`;
  const queryKey=['master-sync',productId];
  const query = useQuery<Job>({queryKey,queryFn:()=>api(path),enabled:open,refetchInterval:1500,retry:false});
  const job=query.data;
  const running=active(job?.status);
  const events=job?.events;
  const visible=useMemo(()=>{
    const needle=search.trim().toLocaleLowerCase();
    return (events || []).filter(event=>(kind==='all' || event.kind===kind) && (!needle || eventText(event).toLocaleLowerCase().includes(needle)));
  },[events,kind,search]);
  const branches=job?.branches || [];
  const passed=branches.filter(row=>row.status==='pass').length;
  const blocked=branches.filter(row=>row.status==='blocked').length;
  const executing=branches.filter(row=>active(row.status)).length;
  const elapsed=job?.started ? duration((running ? Date.now()/1000 : job.updated || Date.now()/1000)-job.started) : undefined;
  const start = async () => {
    if(submitting.current || running || query.isPending || query.isFetching || query.isError)return;
    submitting.current=true;setStarting(true);setError('');
    try {
      const next=await api<Job>(path,{});
      await client.cancelQueries({queryKey});
      client.setQueryData(queryKey,next);
      setSearch('');setKind('all');setFollow(true);
      await query.refetch();
    } catch(e) { setError(errorText(e)); }
    finally { submitting.current=false;setStarting(false); }
  };
  const refresh=()=>{setError('');void query.refetch();};
  const text=(events || []).map(event=>`[${clock(event.at)}] ${eventLabels[event.kind] || event.kind}\n${eventText(event)}`).join('\n\n');
  const copy=async()=>{
    try {await navigator.clipboard.writeText(text);message.success('当前日志已复制');}
    catch {message.error('无法复制日志，请使用下载日志');}
  };
  const download=()=>{
    const url=URL.createObjectURL(new Blob([text+'\n'],{type:'text/plain;charset=utf-8'}));
    const anchor=document.createElement('a');
    anchor.href=url;anchor.download=`master-sync-${job?.id || productId}.log`;anchor.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  useEffect(()=>{
    if(open && follow && log.current)log.current.scrollTop=log.current.scrollHeight;
  },[open,follow,visible]);
  return <>
    <Button aria-label="同步 master" disabled={!enabled} icon={<CodeOutlined/>} onClick={()=>setOpen(true)}>同步 master</Button>
    <Drawer title={<Space><CodeOutlined aria-hidden/>同步 master · 执行终端</Space>} width="min(1120px, 100vw)"
      className="master-sync-drawer" open={open} onClose={()=>setOpen(false)}
      afterOpenChange={shown=>{if(shown && follow && log.current)log.current.scrollTop=log.current.scrollHeight;}}
      extra={<Button aria-label="刷新终端" icon={<ReloadOutlined/>} loading={query.isFetching && !query.isPending} onClick={refresh}>刷新终端</Button>}>
      <div className="master-sync-terminal">
        <section className="master-sync-overview" aria-label="同步概况">
          <div className="master-sync-overview-heading">
            <div><div className="master-sync-eyebrow">分支同步</div><h2>最新 master → feat / release</h2></div>
            <Tag color={job?.status==='blocked'?'warning':job?.status==='completed'?'success':running?'processing':'default'}>
              {starting?'提交中':query.isPending?'读取中':query.isError && !job?'状态未知':labels[job?.status || 'idle'] || job?.status}
            </Tag>
          </div>
          <p>逐个同步分支，冲突先保存方案再处理。关闭终端后，任务继续执行。</p>
          <div className="master-sync-summary">
            <div><strong>{passed}</strong><span>完成</span></div>
            <div className={blocked?'master-sync-warning':''}><strong>{blocked}</strong><span>需处理</span></div>
            <div><strong>{executing}</strong><span>执行中</span></div>
            <div className="master-sync-identity"><span>本次 master</span>{job?.master_commit
              ? <Tooltip title={job.master_commit}><code>{job.master_commit.slice(0,12)}</code></Tooltip> : <span>等待拉取</span>}</div>
          </div>
          <div className="master-sync-controls">
            <div><div role="status">{job?.reason || (query.isPending?'正在读取执行记录…':query.isError?'暂时无法读取执行状态，刷新后再操作。':'打开终端只查看记录，点击开始同步后执行。')}</div>
              {elapsed && <small>耗时 {elapsed}</small>}</div>
            <Button aria-label={!job || job.status==='idle'?'开始同步':'重新同步'} type="primary" icon={<SyncOutlined spin={running}/>} loading={starting}
              disabled={!enabled || running || query.isPending || query.isFetching || query.isError} onClick={()=>void start()}>
              {!job || job.status==='idle'?'开始同步':'重新同步'}
            </Button>
          </div>
        </section>
        {error && <Alert type="error" showIcon message={error}/>}
        {query.error && <Alert type="warning" showIcon message="执行记录暂时无法刷新" description={`${errorText(query.error)} 已保留最近读取的日志，连接恢复前暂不提交新的同步。`}/>}
        <div className="master-sync-workspace">
          <aside className="master-sync-branches" aria-label="分支结果">
            <div className="master-sync-panel-heading"><strong>分支结果</strong><span>已记录 {branches.length} 个</span></div>
            {!branches.length ? <div className="master-sync-branch-empty">{running?'正在获取分支，结果将逐项显示。':'执行后显示各分支结果。'}</div>
              : <ul>{branches.map(row=><li key={row.branch}>
                <div><code>{row.branch}</code><Tag color={row.status==='pass'?'success':row.status==='blocked'?'warning':'processing'}>{labels[row.status] || row.status}</Tag></div>
                <p>{row.reason || '执行中'}</p>
                {row.workspace && <div className="master-sync-conflict-path"><span>保留的冲突工作区</span><code>{row.workspace}</code></div>}
              </li>)}</ul>}
            <div className="master-sync-branch-note">正在执行任务或有未提交改动的分支会保留原状，并记录原因。</div>
          </aside>
          <section className="master-sync-log-panel" aria-label="执行日志">
            <div className="master-sync-panel-heading"><strong><CodeOutlined/> 执行日志</strong>
              <Space size={4}>
                <Tooltip title="复制全部已加载日志"><Button size="small" aria-label="复制日志" icon={<CopyOutlined/>} disabled={!events?.length} onClick={()=>void copy()}/></Tooltip>
                <Tooltip title="下载全部已加载日志"><Button size="small" aria-label="下载日志" icon={<DownloadOutlined/>} disabled={!events?.length} onClick={download}/></Tooltip>
              </Space>
            </div>
            <div className="master-sync-log-tools">
              <Input aria-label="搜索日志" prefix={<SearchOutlined/>} placeholder="搜索命令、日志或方案" allowClear value={search} onChange={event=>setSearch(event.target.value)}/>
              <Select aria-label="日志类型" value={kind} onChange={setKind} options={[
                {value:'all',label:'全部日志'},...Object.entries(eventLabels).map(([value,label])=>({value,label}))]}/>
            </div>
            <div ref={log} role="log" aria-live="off" aria-label="分支同步执行日志" tabIndex={0} className="master-sync-log"
              onScroll={()=>{const node=log.current;if(follow && node && node.scrollHeight-node.clientHeight-node.scrollTop>80)setFollow(false);}}>
              {!visible.length ? <div className="master-sync-log-empty"><CodeOutlined/><strong>{events?.length?'没有匹配的日志':running?'等待执行日志…':'尚无执行日志'}</strong>
                <span>{events?.length?'调整搜索内容或日志类型。':running?'命令、输出和冲突处理方案会显示在这里。':'开始同步后，在这里查看命令和执行输出。'}</span></div>
              : visible.map(event=><div className={`master-sync-event master-sync-event-${event.kind}`} key={`${event.at}:${event.kind}:${event.text}`}>
                  <div className="master-sync-event-meta"><time>{clock(event.at)}</time><span>{eventLabels[event.kind] || event.kind}</span></div>
                  <pre>{eventText(event)}</pre>
                </div>)}
            </div>
            <div className="master-sync-log-footer">
              <span>显示 {visible.length} / {events?.length || 0} 条 · 最近 250 条记录</span>
              <Space size={8}>{!follow && <Button size="small" type="link" onClick={()=>setFollow(true)}>跳到最新</Button>}
                <Switch size="small" aria-label="自动跟随" checked={follow} onChange={setFollow}/><span>自动跟随</span></Space>
            </div>
          </section>
        </div>
      </div>
    </Drawer>
  </>;
}
