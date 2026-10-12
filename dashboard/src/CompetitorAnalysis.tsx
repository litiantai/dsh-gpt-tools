import {useState} from 'react';
import {useInfiniteQuery} from '@tanstack/react-query';
import {App, Button} from 'antd';
import {api} from './api';
import {errorText} from './errors';
import {time} from './components';
import {useProjectNavigation} from './projectNavigation';

type Row={id:string;name?:string;title?:string;reason?:string;urls?:string[];status:string;action?:string;updated:number;version?:number;input_snapshot?:unknown;attempts?:{call_id:string;status:string;reason?:string}[];result?:{diagnostic?:string;detail?:string;failure_kind?:string;outcome?:string;summary?:string;reason?:string;snapshots?:{url:string;status:string;reason?:string}[];fetch_failures?:{url:string;reason:string}[]}};
function useRecords(pid:string,kind:string) {
  const query=useInfiniteQuery<{items:Row[];next_cursor:string|null}>({queryKey:['intelligence',pid,kind],initialPageParam:'',
    queryFn:({pageParam})=>api(`/products/${pid}/intelligence/${kind}?${new URLSearchParams({cursor:String(pageParam),limit:'50'})}`),
    getNextPageParam:page=>page.next_cursor||undefined,refetchInterval:3000,retry:false});
  return {...query,rows:query.data?.pages.flatMap(page=>page.items)||[]};
}

export default function CompetitorAnalysis({pid}:{pid:string}) {
  const {navigate}=useProjectNavigation();
  const competitors=useRecords(pid,'competitors'),jobs=useRecords(pid,'research_jobs');
  const [busy,setBusy]=useState(false);
  const {message}=App.useApp();
  const status:Record<string,string>={active:'启用',disabled:'停用',merged:'已合并',queued:'等待研究',running:'研究中',completed:'已完成',failed:'失败'};
  const start=async(action:string)=>{
    setBusy(true);
    try{await api(`/products/${pid}/intelligence/research_jobs`,{action});await jobs.refetch();message.success('已加入研究队列');}
    catch(e){message.error(errorText(e));}finally{setBusy(false);}
  };
  const pending=(action:string)=>jobs.rows.some(row=>row.action===action&&['queued','running'].includes(row.status));
  return <section className="competitor-analysis">
    <div className="competitor-actions"><Button onClick={()=>navigate('settings','research')}>配置分析范围</Button><Button disabled={busy||pending('find_competitors')} onClick={()=>void start('find_competitors')}>发现新竞品</Button><Button type="primary" disabled={busy||!competitors.rows.some(row=>row.status==='active')||pending('analyze_competitors')} onClick={()=>void start('analyze_competitors')}>开始分析</Button></div>
    <p className="dialogue-muted">查看竞品名单与研究结果。分析模型及自动研究计划在项目设置中配置。</p>
    {[{title:'竞品名单',query:competitors,empty:'暂无竞品，点击“发现新竞品”开始研究。'},{title:'研究记录',query:jobs,empty:'暂无研究记录'}].map(({title,query,empty})=><section key={title}>
      <h2>{title}</h2>
      {query.error && <p role="alert">{errorText(query.error)}</p>}
      {query.isLoading?<p>正在加载…</p>:!query.error&&!query.rows.length&&<p className="dialogue-muted">{empty}</p>}
      {query.rows.map(row=><article className="collaboration-entry" key={row.id}>
        <strong>{row.name||row.title}</strong> · {status[row.status]||row.status}
        {row.result?.outcome && <p>{{partial_failure:'部分失败，已保留可用结果',all_failed:'全部来源失败',no_competitors:'暂无竞品',no_changes:'来源无变化',analyzed:'分析完成',found:'发现竞品'}[row.result.outcome] || row.result.outcome}</p>}
        <p>{row.status==='failed'||row.status==='queued'?row.reason||row.result?.reason:row.result?.summary||row.reason}</p>
        {row.result?.failure_kind&&<p>失败类型：{({authentication:'鉴权问题',connection_timeout:'连接或超时',source_collection:'来源采集失败',invalid_output:'输出无效'} as Record<string,string>)[row.result.failure_kind]||row.result.failure_kind}</p>}
        {(row.result?.diagnostic||row.result?.detail)&&<details><summary>执行器诊断</summary><pre style={{whiteSpace:'pre-wrap'}}>{row.result.diagnostic||row.result.detail}</pre></details>}
        {(row.result?.fetch_failures||row.result?.snapshots?.filter(s=>s.status!=='pass')||[]).map((s,i)=><p key={i}>{s.url}：{s.reason}</p>)}
        {row.input_snapshot!==undefined&&<details><summary>本次配置与尝试记录</summary><pre>{JSON.stringify(row.input_snapshot,null,2)}</pre>{row.attempts?.map(a=><p key={a.call_id}>{a.status}：{a.reason||'完成'}</p>)}</details>}
        {title==='研究记录'&&row.status==='failed'&&<Button disabled={busy} onClick={async()=>{setBusy(true);try{await api(`/products/${pid}/intelligence/research_jobs/${row.id}/retry`,{version:row.version});await jobs.refetch();}catch(e){message.error(errorText(e));}finally{setBusy(false);}}}>重试研究</Button>}
        {(row.urls||[]).filter(url=>/^https?:\/\//i.test(url)).map(url=><p key={url}><a href={url} target="_blank" rel="noreferrer">{url}</a></p>)}
        <small>{time(row.updated)}</small>
      </article>)}
      {query.hasNextPage&&<Button type="text" loading={query.isFetchingNextPage} onClick={()=>void query.fetchNextPage()}>更多历史</Button>}
    </section>)}
  </section>;
}
