import {useState} from 'react';
import {Alert, Button, Card, Collapse, Empty, Table, Tag} from 'antd';
import {useQuery} from '@tanstack/react-query';
import {api} from './api';
import {errorText} from './errors';

type Design={id:string;kind:string;product_id:string;content:{html?:string;fields?:string[];states?:string[];contract?:string}};
type Member={project_id:string;project_name:string;repository:string;role:string;scope:string;status:string;reason?:string;prs:string[];release_pr?:string};
type Group={id:string;version:number;title:string;status:string;reason?:string;members:Member[];designs:Design[]};
const states:Record<string,string>={scoping:'分析协作范围',scope_blocked:'范围分析阻塞',designing:'设计与实现',joint_verifying:'联合验收中',joint_blocked:'联合验收阻塞',joint_queued:'等待联合验收',ready:'联合验收通过',partially_merged:'部分合并',awaiting_final_acceptance:'等待上线复验',completed:'已完成',awaiting_project_delivery:'单项目范围，沿用项目交付'};
export function DesignList({items}:{items:Design[]}) {
  return <Collapse items={items.map(d=>({key:d.id,label:`${d.kind==='ui'?'UI 设计':'接口契约'} · ${d.id.slice(0,8)}`,children:<>
    {d.kind==='ui'&&<iframe title={`UI 预览 ${d.id}`} sandbox="allow-scripts" referrerPolicy="no-referrer" style={{width:'100%',height:600,border:'1px solid #ddd'}} srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">${d.content.html||''}`}/>}
    {d.content.fields&&<ul>{d.content.fields.map((field,i)=><li key={i}>{field}</li>)}</ul>}
    {d.content.contract&&<pre style={{whiteSpace:'pre-wrap'}}>{d.content.contract}</pre>}
  </>}))}/>;
}
export function RunDesigns({runId}:{runId:string}) {
  const query=useQuery<Design[]>({queryKey:['designs',runId],queryFn:()=>api(`/runs/${runId}/designs`),retry:false});
  return <>{query.error&&<Alert type="error" message={errorText(query.error)}/>}<DesignList items={query.data||[]}/>{!query.data?.length&&<Empty description="尚无版本化设计产物"/>}</>;
}
export default function ProjectGroups({pid}:{pid:string}) {
  const query=useQuery<Group[]>({queryKey:['project-groups',pid],queryFn:()=>api(`/products/${pid}/requirement-groups`),refetchInterval:5000});
  const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  return <>
    {(query.error||error)&&<Alert type="error" message={error||errorText(query.error)}/>}
    {!query.data?.length&&<Empty description="暂无关联需求组；在项目设置添加关联项目后，新需求会分析协作范围"/>}
    {query.data?.map(group=><Card key={group.id} title={group.title} extra={<Tag>{states[group.status]||group.status}</Tag>}>
      {group.reason&&<p>{group.reason}</p>}
      {['scope_blocked','joint_blocked'].includes(group.status)&&<Button disabled={busy} onClick={async()=>{setBusy(true);try{await api(`/requirement_groups/${group.id}/retry`,{version:group.version});await query.refetch();setError('');}catch(e){setError(errorText(e));}finally{setBusy(false);}}}>重试当前阶段</Button>}
      <Table rowKey="project_id" pagination={false} dataSource={group.members} columns={[
        {title:'项目',render:(_,m)=><><strong>{m.project_name}</strong><p>{m.repository}</p></>},
        {title:'修改范围',dataIndex:'scope'}, {title:'状态',render:(_,m)=><>{({accepted:'业务验收通过',online:'已上线',delivered:'已交付未上线',blocked:'已阻塞',planning:'设计中',developing:'实现中',queued:'排队中'} as Record<string,string>)[m.status] || m.status}<p>{m.reason}</p></>},
        {title:'项目 PR',render:(_,m)=><>{m.prs.map(url=><p key={url}><a href={url} target="_blank" rel="noreferrer">{m.project_name} · 需求 PR</a></p>)}{m.release_pr&&<a href={m.release_pr} target="_blank" rel="noreferrer">{m.project_name} · Release PR</a>}</>},
      ]}/><DesignList items={group.designs}/>
    </Card>)}
  </>;
}
