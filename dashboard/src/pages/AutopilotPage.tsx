import { ErrorNotice, errorText, serializeError } from '../errors';
import { RecordValue, recordSummary } from '../RecordDetails';
import RecordInspector from '../RecordInspector';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, App, Button, Card, Drawer, Form, Input, Modal, Select, Space, Table, Tag, Tabs, Tooltip } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { ArrowRightOutlined, ReloadOutlined, PlusOutlined } from '@ant-design/icons';
import { api } from '../api';
import { time, useData, eventLabels, labels as sharedLabels } from '../components';
import ProjectFlow, { type FlowNodeId } from '../ProjectFlow';
import ProjectEvidence from '../ProjectEvidence';
import DeliveryBoard, {type DeliveryBoardData} from '../DeliveryBoard';
import DeliveryTasks from '../DeliveryTasks';
import ProjectSettings from '../ProjectSettings';
import ProjectIntelligence from '../ProjectIntelligence';
import RepositoryScans from '../RepositoryScans';
import TodayQuota from '../TodayQuota';
import MasterSyncTerminal from '../MasterSyncTerminal';
import { projectSections as sections, useProjectNavigation, type ProjectSection } from '../projectNavigation';

interface RecordItem {
  id: string; product_id: string; status: string; version: number; created: number; updated: number;
  name?: string; title?: string; summary?: string; reason?: string; goal?: string; source?: string;
  requirement_id?: string; run_id?: string; release_id?: string; review_id?: string;
  [key: string]: unknown;
}
const labels: Record<string,string> = {
  ...sharedLabels,
  investigating:'调查取证中',awaiting_external:'等待外部条件（自动复查）',resolved:'调查确认已解决',
  accepted:'业务验收通过（待交付）', delivered:'已交付未上线', online:'已上线', preparing:'整合验收成果', code_review:'Code Review 中', review_failed:'代码评审失败', merging_feature:'评审通过 · 合入 release', collecting:'等待 23:30 统一验证', validating:'release 统一验证', syncing_feature:'同步评审 MR', repairing_feature:'修复评审问题', syncing_release:'同步上线 MR', reviewing_release:'release 变更复审', repairing_release:'修复统一验证问题', release_failed:'统一验证未通过', repairing:'问题修复中', awaiting_merge:'待合并（等待收口）', syncing:'同步 master', merging:'合并确认中', paused:'已暂停', active:'自主运行', pending:'待评估', classified:'已归因', queued:'待开发',
  planning:'制定方案', plan_review:'方案审查', developing:'开发中', verifying:'验证中',
  acceptance_review:'验收审查', awaiting_release:'等待空闲发布', deploying:'发布中', observing:'观察中',
  completed:'已完成', blocked:'已阻塞', cancelled:'已取消', rolled_back:'已回滚', rollback_pending:'等待回滚',
  pausing:'正在暂停', cancelling:'正在取消', registered:'已登记',
  pass:'通过', fail:'失败', stale:'提交已变化（待重评）', skipped:'已跳过', superseded:'已纠正', recorded:'已记录',
};

function agentExecutionError(record:RecordItem) {
  const result=record.result as {failure_kind?:string;reason?:string} | undefined;
  return result?.failure_kind==='agent_execution' || record.status==='blocked' && /模型执行失败|Harness (执行失败|审查执行失败)|Agent 回执读取失败|最终回执解析失败|执行进程已失联|执行超时/.test(String(record.reason || result?.reason || ''));
}

export default function AutopilotPage() {
  const products=useData<RecordItem[]>('/products');
  const signals=useData<RecordItem[]>('/signals');
  const requirements=useData<RecordItem[]>('/requirements');
  const runs=useData<RecordItem[]>('/runs');
  const releases=useData<RecordItem[]>('/releases');
  const deliveries=useData<RecordItem[]>('/deliveries');
  const [params,setParams]=useSearchParams();
  const project=products.data?.find(p=>p.id===params.get('project')) ?? products.data?.[0];
  const deliveryBoard=useQuery<DeliveryBoardData>({queryKey:['delivery-board',project?.id],
    queryFn:()=>api(`/products/${project!.id}/delivery-board`),enabled:!!project,refetchInterval:5000,retry:false});
  const gitUrl=(project?.git as {url?:string})?.url;
  const gitStatus=useQuery<{status:string;code:string;reason:string;checked_at:number}>({
    queryKey:['git-status',project?.id,gitUrl],
    queryFn:()=>api(`/products/${project!.id}/git-status`),
    enabled:!!gitUrl,refetchInterval:60000,staleTime:30000,retry:false,
  });
  const blockedDelivery=deliveries.data?.find(d=>d.product_id===project?.id && d.status==='blocked');
  const gitIssue=gitUrl && (blockedDelivery?.reason || (gitStatus.error?'无法读取本地运行时 GitHub 认证状态，请重新检查。':gitStatus.data?.status==='blocked'?gitStatus.data.reason:undefined));
  const gitIssueSummary=blockedDelivery?'代码交付已阻塞，需继续处理':gitStatus.data?.code==='missing_credentials'?'缺少 GitHub 凭据，交付已阻塞':gitStatus.data?.code==='insufficient_permissions'?'GitHub 权限不足，交付已阻塞':'GitHub 交付被阻塞';
  const workbench=useData<{sessions:RecordItem[];reviews:RecordItem[];events:RecordItem[]}>(project?`/products/${project.id}/workbench`:'/autopilot/workbench-empty');
  const [node,setNode]=useState<FlowNodeId>();
  const {section,tab,navigate,setTab}=useProjectNavigation();
  const { message }=App.useApp();
  const [selection,setSelection]=useState<{record:RecordItem;kind:string;receipt?:Record<string,unknown>}>();
  const selected=selection?.record;
  const setSelected=(record:RecordItem|undefined,kind='')=>setSelection(record?{record,kind:kind==='online_requirements'?'requirements':kind}:undefined);
  const [feedback,setFeedback]=useState(false);
  const [scanning,setScanning]=useState(false);
  const [filters,setFilters]=useState<Record<string,{query?:string;status?:string}>>({});
  const [busy,setBusy]=useState(false);
  const [form]=Form.useForm();
  const reload=()=>Promise.all([products.refetch(),signals.refetch(),requirements.refetch(),runs.refetch(),releases.refetch(),deliveries.refetch(),deliveryBoard.refetch(),workbench.refetch(),...(gitUrl?[gitStatus.refetch()]:[])]);
  const act=async(kind:string,item:RecordItem,action:string)=>{
    setBusy(true);
    try { await api(`/${kind}/${item.id}/${action}`,{version:item.version}); await reload(); message.success('操作已记录'); }
    catch(e) { message.error(errorText(e)); }
    finally { setBusy(false); }
  };
  const colors=(status:string)=>['blocked','fail','review_failed','release_failed'].includes(status)?'error':status==='completed'?'success':['active','developing','deploying','observing'].includes(status)?'processing':'default';
  const recordTitle=(r:RecordItem)=>r.title || r.name || (r.summary?recordSummary(r.summary):r.id.slice(0,8));
  const table=(kind:string,data:RecordItem[]|undefined)=> {
    const isRequirement=kind==='requirements' || kind==='online_requirements';
    const linkedRun=(r:RecordItem)=>kind==='requirements' ? runs.data?.find(run=>run.product_id===r.product_id && run.id===r.run_id) : undefined;
    const currentStatus=(r:RecordItem)=>linkedRun(r)?.status || r.status;
    const currentReason=(r:RecordItem)=>linkedRun(r)?.reason || r.reason || (r.result as {reason?:string;summary?:string} | undefined)?.reason || (r.result as {summary?:string} | undefined)?.summary || r.monitor_reason || '—';
    const filterKey=`${project?.id}:${kind}`;
    const filter=filters[filterKey] || {};
    const updateFilter=(value:{query?:string;status?:string})=>setFilters(old=>({...old,[filterKey]:{...filter,...value}}));
    const rows=(data ?? []).filter(r=>(!filter.status || currentStatus(r)===filter.status) &&
      (!filter.query || [recordTitle(r),currentReason(r),r.id].some(value=>String(value || '').toLowerCase().includes(filter.query!.toLowerCase()))));
    return <>
    <Space wrap className="project-record-filters">
      <Input.Search aria-label="搜索当前记录" placeholder="搜索名称、原因或编号" allowClear value={filter.query || ''} onChange={e=>updateFilter({query:e.target.value})} style={{width:260}}/>
      <Select aria-label="筛选记录状态" placeholder="全部状态" allowClear value={filter.status} onChange={status=>updateFilter({status})} style={{width:150}} options={[...new Set(data?.map(currentStatus))].filter(Boolean).map(status=>({value:status,label:labels[status] || status}))}/>
      <span className="muted">{rows.length} 条{isRequirement?'需求':'记录'}</span>
    </Space>
    <Table<RecordItem>
    style={kind==='deliveries'?{containerType:'inline-size'}:undefined}
    expandable={kind==='deliveries'?{expandedRowRender:r=><DeliveryTasks deliveryId={r.id} day={String(r.day || '')}/>,columnWidth:110,columnTitle:'当日问题',expandIcon:({expanded,onExpand,record})=><Button size="small" aria-expanded={expanded} onClick={e=>onExpand(record,e)}>{expanded?'收起问题':'展开问题'}</Button>}:undefined}
    className="project-record-table" tableLayout="fixed" rowKey="id" dataSource={rows} size="middle" pagination={{pageSize:10,showSizeChanger:false}} scroll={{x:1050+(kind==='requirements'?450:kind==='deliveries'?180:0)}}
    columns={[
      {title:isRequirement?'需求名称':'名称 / 摘要',width:240,render:(_,r)=><button className="text-link project-record-name" onClick={()=>setSelected(r,kind)}>{kind==='events'?(eventLabels[recordTitle(r)] || recordTitle(r)):recordTitle(r)}</button>},

      {title:kind==='requirements'?'当前进度':'状态',width:140,render:(_,r)=><Tag color={colors(currentStatus(r))}>{agentExecutionError(r)?'Agent 运行异常':labels[currentStatus(r)] || currentStatus(r)}</Tag>},
      {title:'原因',render:(_,r)=><><ErrorNotice value={currentReason(r)}/>{r.status==='blocked' && !!r.next_auto_retry_at && <div className="muted">{r.auto_retry_wait_reason?String(r.auto_retry_wait_reason):`异常自动重试：${time(Number(r.next_auto_retry_at))}（有额度且执行空闲时）`}</div>}</>},
      ...(kind==='deliveries'?[{title:'交付分支',width:180,render:(_:unknown,r:RecordItem)=>String(r.branch || '—')}]:[]),
      ...(kind==='requirements'?[{title:'处理分类',width:150,render:(_:unknown,r:RecordItem)=><RecordValue value={r.classification} field="classification"/>},{title:'下次调查',width:150,render:(_:unknown,r:RecordItem)=>r.classification!=='development' && ['pending','awaiting_external'].includes(r.status) && r.next_investigation?time(Number(r.next_investigation)):'—'},{title:'需求归属日期',width:150,render:(_:unknown,r:RecordItem)=>String(r.requirement_day || '历史需求')}]:[]),
      {title:'更新时间',width:150,render:(_,r)=>time(r.updated)},
      {title:'操作',width:200,render:(_,r)=><Space wrap>
        {kind==='requirements' && r.status==='pending' && (!r.classification || r.classification==='development') && <Button size="small" onClick={()=>void act(kind,r,'queue')}>加入开发队列</Button>}
        {kind==='runs' && !['accepted','delivered','online','completed','cancelled','rolled_back','blocked'].includes(r.status) && <>
          <Button size="small" onClick={()=>void act(kind,r,'pause')}>暂停</Button>
          <Button size="small" danger onClick={()=>void act(kind,r,'cancel')}>取消</Button>
        </>}
        {kind==='runs' && r.status==='blocked' && <Button size="small" disabled={r.uncertain===true} onClick={()=>void act(kind,r,'retry')}>重试</Button>}
        {kind==='runs' && r.status==='accepted' && !(project?.git as {url?:string})?.url && <Button size="small" onClick={()=>void act(kind,r,'release')}>进入发布</Button>}
        {kind==='releases' && ['completed','observing','blocked'].includes(r.status) && <Button size="small" danger onClick={()=>void act(kind,r,'rollback')}>回滚</Button>}
        {kind==='deliveries' && r.status==='blocked' && <Button size="small" onClick={()=>void act(kind,r,'retry')}>继续处理</Button>}
        {typeof r.feature_pr_url==='string' && r.feature_pr_url.startsWith('https://github.com/') && <a href={r.feature_pr_url} target="_blank" rel="noreferrer">代码评审 MR</a>}
        {typeof r.pr_url==='string' && r.pr_url.startsWith('https://github.com/') && <a href={r.pr_url} target="_blank" rel="noreferrer">查看 PR</a>}
        <Button size="small" onClick={()=>setSelected(r,kind)}>详情</Button>
      </Space>},
    ]}/></>;
  };
  const error=products.error || signals.error || requirements.error || runs.error || releases.error || deliveries.error || deliveryBoard.error || workbench.error;
  const scoped=(items:RecordItem[]|undefined)=>items?.filter(r=>r.product_id===project?.id) ?? [];
  const projectRuns=scoped(runs.data);
  const designRuns=projectRuns.filter(r=>typeof r.plan==='string' && r.plan.trim().length>0 && ['queued','planning','plan_review'].includes(r.status));
  const projectRequirements=scoped(requirements.data);
  const projectReleases=scoped(releases.data);
  const projectDeliveries=scoped(deliveries.data);
  const onlineRequirements=projectRequirements.filter(r=>r.status==='online').map(requirement=>{
    const delivery=projectDeliveries.find(d=>d.id===requirement.delivery_id);
    const review=deliveryBoard.data?.prs.find(pr=>pr.requirement_id===requirement.id);
    return {...requirement,pr_url:requirement.pr_url || delivery?.pr_url,feature_pr_url:requirement.feature_pr_url || review?.pr_url};
  });
  const deliveryStages:Record<string,string[]>={online:['online'],merge:['awaiting_merge','syncing','merging','collecting','validating','merging_feature','syncing_release','syncing_feature']};
  const stageDeliveries=(stage:string)=>projectDeliveries.filter(r=>deliveryStages[stage]?.includes(String(r.status==='blocked'?r.resume_status:r.status)));
  const blockedDeliveries=projectDeliveries.filter(r=>r.status==='blocked');
  const count=(...statuses:string[])=>projectRuns.filter(r=>statuses.includes(r.status)).length;
  const observations=['probe','inspect',...((project?.git as {enabled?:boolean})?.enabled?['master_sync','final_acceptance']:[]),'discover'].map(action=>{
    const receipts=(project?.receipts ?? []) as {action:string;at:number;result:RecordItem}[];
    const receipt=[...receipts].reverse().find(r=>r.action===action || action==='probe' && r.action==='recover-runtime');
    const result=(project?.[`last_${action}_result`] ?? receipt?.result) as RecordItem | undefined;
    return {action,receipt,title:({probe:'运行监测',inspect:(project?.git as {enabled?:boolean})?.enabled?(!result || (result.instance as {instance_role?:string})?.instance_role==='master'?'master 巡检':'隔离巡检（历史）'):'隔离巡检',master_sync:'master 实例更新',final_acceptance:'master 最终验收',discover:'需求发现'} as Record<string,string>)[action],result,at:Number(project?.[`last_${action}`] ?? receipt?.at ?? 0)};
  });
  const issues=observations.filter(o=>o.result && (['blocked','fail'].includes(o.result.status) || o.result.monitor_mode==='basic'));
  const showMonitoring=()=>{setTab('monitoring');document.getElementById('project-records')?.scrollIntoView({behavior:'smooth'});};
  const counts:Partial<Record<FlowNodeId,number>>={
    product:projectRequirements.length,signals:scoped(signals.data).filter(s=>s.status==='pending').length,
    requirements:projectRequirements.filter(r=>['pending','investigating','awaiting_external'].includes(r.status)).length,
    review:count('plan_review'),planning:count('queued','planning'),design:designRuns.length,
    development:count('developing'),testing:count('verifying'),acceptance:count('acceptance_review','accepted'),
    delivery:count('delivered'),online:onlineRequirements.length,
    codeReview:new Set((deliveryBoard.data?.prs || []).filter(pr=>pr.status==='unreviewed' && pr.git_status==='open' && pr.requirement_id).map(pr=>pr.requirement_id)).size,fixing:deliveryBoard.data?.issues.length ?? 0,merge:deliveryBoard.data?.release_prs?.filter(pr=>pr.git_status==='open').length ?? 0,
    blocked:count('blocked','pausing')+blockedDeliveries.length,cancelled:count('cancelled','rolled_back'),
  };
  const nodeStatuses:Partial<Record<FlowNodeId,string[]>>={review:['plan_review'],planning:['queued','planning'],development:['developing'],testing:['verifying'],acceptance:['acceptance_review','accepted'],delivery:['delivered'],blocked:['blocked','pausing'],cancelled:['cancelled','rolled_back']};
  const selectedKind=node==='signals'?'signals':node==='requirements'?'requirements':node==='online'?'online_requirements':node==='merge'?'deliveries':'runs';
  const selectedData=node==='product'?projectRuns:node==='design'?designRuns:node==='signals'?scoped(signals.data).filter(s=>s.status==='pending'):node==='requirements'?projectRequirements.filter(r=>['pending','investigating','awaiting_external'].includes(r.status)):node==='online'?onlineRequirements:node==='merge'?stageDeliveries(node):projectRuns.filter(r=>nodeStatuses[node ?? 'product']?.includes(r.status));
  if(section==='intelligence')return <ProjectIntelligence pid={project?.id} projects={products.data}/>;
  return <>
    {scanning && <RepositoryScans onClose={()=>setScanning(false)} productId={project?.id} onChanged={reload}/>}
    <div className="project-page-heading">
      <div><div className="project-eyebrow">持续研发 / {sections[section].label}</div><div className="project-title-row"><h1>{project?.name || '持续研发控制中心'}</h1><Tag>{project?(project.status==='observing'?'仅监测':labels[project.status] || project.status):'尚未接入项目'}</Tag></div><p>{project?.goal || '从巡检发现到上线观察，每一次推进都有据可查。'}</p></div>
      <Space>{project && <MasterSyncTerminal key={project.id} productId={project.id} enabled={!!(project.git as {enabled?:boolean})?.enabled && !!project.delivery_repository}/>}<Button onClick={()=>setScanning(true)}>接入仓库</Button><Select aria-label="选择项目" value={project?.id} placeholder="选择项目" style={{width:160}} options={products.data?.map(p=>({label:p.name,value:p.id}))} onChange={id=>{setParams(p=>{p.set('project',id);return p;});setNode(undefined);setSelected(undefined);}}/><Button icon={<PlusOutlined/>} onClick={()=>{form.setFieldValue('product',project?.id);setFeedback(true);}}>记录需求</Button><Button aria-label="刷新项目" icon={<ReloadOutlined/>} onClick={()=>void reload()}/></Space>
    </div>
    {project?.runtime_recovery_state && <Alert type="info" showIcon message={String((project.runtime_recovery_state as Record<string,unknown>).message || '应用恢复状态已更新')} description={<>尝试次数：{Number((project.runtime_recovery_state as Record<string,unknown>).attempts || 0)} · 下次检查：{time(Number((project.runtime_recovery_state as Record<string,unknown>).next_check_at || 0))}</>}/>}
    {error && <Alert type="error" showIcon message={<ErrorNotice value={error}/>}/>}
    {project && <TodayQuota key={project.id} productId={project.id} reload={reload}/>}
    <Tabs className="project-primary-tabs" activeKey={section} items={Object.entries(sections).map(([key,value])=>({key,label:value.label}))} onChange={key=>navigate(key as ProjectSection)}/>
    {section==='overview' && <>
    {gitIssue && <Alert id="project-git-issue" className="project-git-alert" type="error" showIcon message={<Tooltip title={gitIssue}><span tabIndex={0}>{gitIssueSummary}</span></Tooltip>} action={<Space size={4}><Button type="link" size="small" onClick={()=>navigate('settings','git')}>配置 Token</Button>{blockedDelivery?<Button type="link" size="small" disabled={busy} onClick={()=>void act('deliveries',blockedDelivery,'retry')}>继续处理</Button>:<Button type="text" size="small" aria-label="重新检查" title="重新检查" icon={<ReloadOutlined/>} loading={gitStatus.isFetching} onClick={()=>void gitStatus.refetch()}/>}</Space>}/>}
    <ProjectFlow counts={counts} selected={node} onSelect={setNode} projectName={project?.name || '未接入项目'}/>
    <div className="project-summary-bar">
      <button onClick={()=>{setTab('sessions');document.getElementById('project-records')?.scrollIntoView({behavior:'smooth'});}}><span>执行会话</span><strong>{workbench.data?.sessions.length ?? '—'}</strong><small>当前项目的真实会话</small><ArrowRightOutlined/></button>
      <button onClick={()=>{setTab('reviews');document.getElementById('project-records')?.scrollIntoView({behavior:'smooth'});}}><span>监工审查</span><strong>{workbench.data?.reviews.length ?? '—'}</strong><small>方案 · 检查 · 验收</small><ArrowRightOutlined/></button>
      <button onClick={()=>gitIssue?document.getElementById('project-git-issue')?.scrollIntoView({behavior:'smooth'}):issues.length?showMonitoring():setNode('blocked')}><span>需要关注</span><strong>{count('blocked','pausing')+blockedDeliveries.length+issues.length+(gitIssue?1:0)}</strong><small>{gitIssue?gitIssueSummary:(issues[0]?.result?errorText(issues[0].result,'应用服务'):'阻塞与返修任务')}</small><ArrowRightOutlined/></button>
      <button onClick={()=>setTab('policy')}><span>运行策略</span><strong className="summary-word">{project?.status==='active'?'自主运行':project?.status==='observing'?'仅监测':'已暂停'}</strong><small>{project?.status==='active'?'检查通过后自动推进':'自动开发与发布尚未启用'}</small><ArrowRightOutlined/></button>
    </div>
    <div className="project-live-note"><span><i/>{project?.last_probe?`最近巡检 ${time(Number(project.last_probe))}`:'尚无巡检回执'}</span><span>节点数字来自当前项目台账 · 点击节点查看证据</span></div>
    </>}
    <Card className="panel project-records" id="project-records">{section==='evidence'?<ProjectEvidence key={project?.id} productId={project?.id} tab={tab} onTabChange={setTab}/>:section==='settings'?<ProjectSettings key={project?.id} project={project} reload={reload} tab={tab} onTabChange={setTab} hasRunningTasks={projectRuns.some(r=>!['accepted','delivered','online','completed','cancelled','rolled_back','queued','blocked'].includes(r.status))}/>:<Tabs activeKey={sections[section]?.tabs.includes(tab)?tab:sections[section]?.tabs[0]} onChange={setTab} items={[
      {key:'monitoring',label:`运行状态${issues.length?` · ${issues.length} 项需关注`:''}`,children:<Space direction="vertical" style={{width:'100%'}}>{observations.map(o=><Alert key={o.action} showIcon type={!o.result?'info':o.result.monitor_mode==='basic'?'warning':o.result.status==='pass'?'success':o.result.status==='blocked'?'warning':o.result.status==='busy'?'info':'error'} message={`${o.title} · ${o.result?(o.result.monitor_mode==='basic'?'基础健康正常 · 发布监测未接入':o.result.status==='busy'?'等待执行':labels[o.result.status] || o.result.status):'尚未执行'}`} description={<><div>{o.result && o.result.status!=='pass'?<ErrorNotice value={o.result} subject="应用服务"/>:String(o.result?.reason || (o.result?.status==='pass'?'本次检查已通过':'等待执行回执'))}</div>{o.at>0 && <small>{time(o.at)}</small>}{o.result && <Button type="link" size="small" onClick={()=>setSelection({record:o.result!,kind:'monitoring',receipt:o.receipt && JSON.stringify(o.receipt.result)===JSON.stringify(o.result)?o.receipt:{action:o.action,at:o.at,result:o.result}})}>查看回执</Button>}</>}/>)}</Space>},
      {key:'requirements',label:`需求台账 ${projectRequirements.length}`,children:table('requirements',projectRequirements)},
      {key:'signals',label:`监测信号 ${scoped(signals.data).length}`,children:table('signals',scoped(signals.data))},
      {key:'runs',label:`任务记录 ${projectRuns.length}`,children:table('runs',projectRuns)},
      {key:'deliveries',label:`代码交付 ${projectDeliveries.length}`,children:table('deliveries',projectDeliveries)},
      {key:'code_reviews',label:'代码评审 · 需求 PR',children:<DeliveryBoard key={`prs-${project?.id}`} data={deliveryBoard.data} kind="prs" loading={deliveryBoard.isLoading}/>},
      {key:'release_prs',label:'待合并 · Release PR',children:<DeliveryBoard key={`release-prs-${project?.id}`} data={deliveryBoard.data} kind="release_prs" loading={deliveryBoard.isLoading}/>},
      {key:'repair_issues',label:'问题修复',children:<DeliveryBoard key={`issues-${project?.id}`} data={deliveryBoard.data} kind="issues" loading={deliveryBoard.isLoading}/>},
      {key:'releases',label:`客户端安装 ${projectReleases.length}`,children:table('releases',projectReleases)},
      {key:'sessions',label:'执行会话',children:table('sessions',workbench.data?.sessions)},
      {key:'reviews',label:'监工审查',children:table('reviews',workbench.data?.reviews)},
      {key:'events',label:'项目日志',children:table('events',workbench.data?.events)},
    ].filter(item=>sections[section]?.tabs.includes(item.key))}/>}</Card>
    <Drawer title={node==='codeReview'?'代码评审 · 需求 PR':node==='merge'?'待合并 · Release PR':node==='online'?'已上线 · 需求记录':node==='fixing'?'问题修复 · 未完成问题':'链路节点 · 任务与证据'} open={!!node} onClose={()=>setNode(undefined)} width="min(1040px, 94vw)">
      {node==='codeReview' || node==='fixing' || node==='merge'?<DeliveryBoard key={`${node}-${project?.id}`} data={deliveryBoard.data} kind={node==='codeReview'?'prs':node==='merge'?'release_prs':'issues'} loading={deliveryBoard.isLoading}/>:<>
        {selectedData.length===0 && !(node==='blocked' && blockedDeliveries.length) && <Alert type="info" showIcon message="该节点当前无待处理任务"/>}
        {table(selectedKind,selectedData)}
        {node==='blocked' && blockedDeliveries.length>0 && <><h3>代码交付阻塞</h3>{table('deliveries',blockedDeliveries)}</>}
      </>}
    </Drawer>
    <Modal title={selected?.title || selected?.name || '证据与关联记录'} open={!!selected} width={850} footer={null} destroyOnHidden onCancel={()=>setSelected(undefined)}>
      {selected && selection && <RecordInspector key={`${selection.kind}:${selected.id || selection.receipt?.action || ''}`} record={selected} kind={selection.kind} receipt={selection.receipt}
        statusLabel={agentExecutionError(selected)?'Agent 运行异常':selected.monitor_mode==='basic'?'基础健康正常 · 发布监测未接入':undefined}/>}
    </Modal>
    <Modal title="提交产品反馈" open={feedback} confirmLoading={busy} onCancel={()=>setFeedback(false)} onOk={async()=>{
      try {const v=await form.validateFields();setBusy(true);await api('/signals',{product_id:v.product,signal:{source:'feedback',code:crypto.randomUUID(),summary:v.summary,evidence:v.evidence,component:'finance-assistant'}});setFeedback(false);form.resetFields();await reload();}
      catch(e){message.error(errorText(e));}finally{setBusy(false);}
    }}><Form form={form} layout="vertical">
      <Form.Item name="product" label="产品" rules={[{required:true}]}><Select options={products.data?.map(p=>({label:p.name,value:p.id}))}/></Form.Item>
      <Form.Item name="summary" label="问题或改进建议" rules={[{required:true}]}><Input/></Form.Item>
      <Form.Item name="evidence" label="复现步骤、实际表现和预期结果" rules={[{required:true}]}><Input.TextArea rows={5}/></Form.Item>
    </Form></Modal>
  </>;
}
