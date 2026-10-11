import {useState} from 'react';
import {useInfiniteQuery} from '@tanstack/react-query';
import {App, Button} from 'antd';
import {api} from './api';
import {errorText} from './errors';
import {time} from './components';

type Row={id:string;name?:string;title?:string;reason?:string;urls?:string[];status:string;action?:string;updated:number;result?:{summary?:string;reason?:string}};
function useRecords(pid:string,kind:string) {
  const query=useInfiniteQuery<{items:Row[];next_cursor:string|null}>({queryKey:['intelligence',pid,kind],initialPageParam:'',
    queryFn:({pageParam})=>api(`/products/${pid}/intelligence/${kind}?${new URLSearchParams({cursor:String(pageParam),limit:'50'})}`),
    getNextPageParam:page=>page.next_cursor||undefined,refetchInterval:3000,retry:false});
  return {...query,rows:query.data?.pages.flatMap(page=>page.items)||[]};
}

export default function CompetitorAnalysis({pid}:{pid:string}) {
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
    <div className="competitor-actions"><Button disabled={busy||pending('find_competitors')} onClick={()=>void start('find_competitors')}>发现新竞品</Button><Button type="primary" disabled={busy||!competitors.rows.some(row=>row.status==='active')||pending('analyze_competitors')} onClick={()=>void start('analyze_competitors')}>开始分析</Button></div>
    <p className="dialogue-muted">查看竞品名单与研究结果。分析模型及自动研究计划在项目设置中配置。</p>
    {[{title:'竞品名单',query:competitors,empty:'暂无竞品，点击“发现新竞品”开始研究。'},{title:'研究记录',query:jobs,empty:'暂无研究记录'}].map(({title,query,empty})=><section key={title}>
      <h2>{title}</h2>
      {query.error && <p role="alert">{errorText(query.error)}</p>}
      {query.isLoading?<p>正在加载…</p>:!query.error&&!query.rows.length&&<p className="dialogue-muted">{empty}</p>}
      {query.rows.map(row=><article className="collaboration-entry" key={row.id}>
        <strong>{row.name||row.title}</strong> · {status[row.status]||row.status}
        <p>{row.result?.summary||row.result?.reason||row.reason}</p>
        {(row.urls||[]).filter(url=>/^https?:\/\//i.test(url)).map(url=><p key={url}><a href={url} target="_blank" rel="noreferrer">{url}</a></p>)}
        <small>{time(row.updated)}</small>
      </article>)}
      {query.hasNextPage&&<Button type="text" loading={query.isFetchingNextPage} onClick={()=>void query.fetchNextPage()}>更多历史</Button>}
    </section>)}
  </section>;
}
