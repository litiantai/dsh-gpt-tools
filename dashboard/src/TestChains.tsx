import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, App, Button, Card, Checkbox, Drawer, Form, Image, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd';
import { api } from './api';
import { useData, time } from './components';
import { errorText } from './errors';
import { ReviewerSelect } from './ReviewerSelect';

interface Step { id:string; goal:string; expected:string; loop:boolean; observation_action:'none'|'reload'|'navigate'; observation_url?:string; acceptance_indices:number[] }
interface Definition { title:string; steps:Step[] }
interface Chain { id:string; version:number; title:string; status:string; requirement_ids:string[]; current_version_id?:string; baseline_id?:string; reason?:string }
interface Version { id:string; chain_id:string; number:number; definition:Definition }
interface Run { id:string; version:number; title:string; status:string; reason?:string; created:number; current_step?:string; observations?:number; actions?:number; tokens?:number; next_observation_at?:number; baseline_confirmed?:boolean; target:{branch?:string;commit?:string}; snapshots:{chain_id:string;version_id:string;definition:Definition;baseline_id?:string}[]; evidence?:Evidence[] }
interface Evidence { id:string; title:string; action:string; expected:string; actual:string; status:string; at:number; screenshot?:string }
interface Config { enabled:boolean; model?:Record<string,unknown>; interval_seconds:number; timeout_seconds:number; max_observations:number; max_actions:number; allowed_origins?:string[]; storage_state?:string }
interface Data { chains:Chain[]; versions:Version[]; runs:Run[]; baselines:{id:string;run_id:string;snapshot:{version_id:string};confirmed_at:number}[]; config:Config; product_version:number; driver:string }
interface Requirement { id:string; product_id:string; title:string; status:string; acceptance?:string[] }
interface Task { id:string; product_id:string; title:string; branch?:string; commit?:string; workspace?:string }
const labels:Record<string,string> = {queued:'等待执行',running:'执行中',observing:'持续观测',cancelling:'正在停止',cancelled:'已停止',pass:'通过',fail:'失败',blocked:'已阻塞',timeout:'超时',ready:'可复用',recorded:'已记录'};
const terminal = ['pass','fail','blocked','cancelled','timeout'];
const statusTag = (s:string) => <Tag color={s==='pass'?'success':['fail','blocked','timeout'].includes(s)?'error':['running','observing'].includes(s)?'processing':'default'}>{labels[s] || s}</Tag>;

function Screenshot({id}:{id:string}) {
  const result=useQuery<{data_url:string}>({queryKey:['evidence-screenshot',id],queryFn:()=>api(`/evidence/${id}/screenshot`),staleTime:Infinity,retry:false});
  return result.error?<Alert type="error" message={errorText(result.error)}/>:result.data?<Image src={result.data.data_url} alt="实际界面截图" width="100%"/>:<span>截图加载中…</span>;
}

function BaselinePreview({prefix,runId}:{prefix:string;runId:string}) {
  const result=useQuery<Run>({queryKey:['test-chain-baseline',runId],queryFn:()=>api(`${prefix}/runs/${runId}`),staleTime:Infinity,retry:false});
  return <details><summary>展开已确认的正确结果</summary>{result.error&&<Alert type="error" message={errorText(result.error)}/>}<Space align="start" wrap>{result.data?.evidence?.filter(e=>e.status==='pass').map(e=><Card key={e.id} size="small" title={e.title} style={{maxWidth:440}}><p>{e.actual}</p>{e.screenshot&&<Screenshot id={e.id}/>}</Card>)}</Space></details>;
}

export default function TestChains({pid,tab,onTabChange}:{pid:string;tab:string;onTabChange:(tab:string)=>void}) {
  const prefix=`/products/${pid}/test-chains`;
  const data=useQuery<Data>({queryKey:['test-chains',pid],queryFn:()=>api(prefix),refetchInterval:2500,retry:false});
  const requirements=useData<Requirement[]>('/requirements');
  const tasks=useData<Task[]>('/runs');
  const [selectedRun,setSelectedRun]=useState<string>();
  const details=useQuery<Run>({queryKey:['test-chain-run',pid,selectedRun],queryFn:()=>api(`${prefix}/runs/${selectedRun}`),enabled:!!selectedRun,refetchInterval:2500,retry:false});
  const [busy,setBusy]=useState(false);
  const [editing,setEditing]=useState<Chain>();
  const [running,setRunning]=useState<Chain>();
  const [linking,setLinking]=useState<Chain>();
  const [requirement,setRequirement]=useState<string>();
  const [dirty,setDirty]=useState(false);
  const [configVersion,setConfigVersion]=useState<number>();
  const [editForm]=Form.useForm();
  const [runForm]=Form.useForm();
  const [linkForm]=Form.useForm();
  const [configForm]=Form.useForm();
  const {message}=App.useApp();
  const value=data.data;
  useEffect(()=>{ if(value && !dirty){ configForm.setFieldsValue({...value.config,origins:(value.config.allowed_origins || []).join('\n')});setConfigVersion(value.product_version); } },[value?.product_version,dirty,configForm]);
  const reqs=requirements.data?.filter(r=>r.product_id===pid && !['pending_confirmation','rejected'].includes(r.status)) || [];
  const options=reqs.map(r=>({value:r.id,label:r.title}));
  async function act(path:string,body:Record<string,unknown>) {
    setBusy(true);
    try { const result=await api(path,body);await data.refetch();await details.refetch();return result; }
    catch(error){message.error(errorText(error));throw error;}
    finally{setBusy(false);}
  }
  const perform=(path:string,body:Record<string,unknown>)=>{void act(path,body).catch(()=>{});};
  function edit(chain:Chain) {
    const version=value?.versions.find(v=>v.id===chain.current_version_id);
    editForm.resetFields();editForm.setFieldsValue({definition:version?.definition,requirement_id:chain.requirement_ids[0],replacements:[]});setEditing(chain);
  }
  async function saveEdit() {
    const fields=await editForm.validateFields();
    const replacements=Object.fromEntries((fields.replacements || []).filter((x:{old?:string;next?:string})=>x.old&&x.next).map((x:{old:string;next:string})=>[x.old,x.next]));
    await act(`${prefix}/${editing!.id}/revise`,{version:editing!.version,...fields,replacements});setEditing(undefined);
  }
  async function saveConfig() {
    const fields=await configForm.validateFields();const {origins,...config}=fields;
    config.allowed_origins=String(origins || '').split('\n').map(x=>x.trim()).filter(Boolean);
    if(!config.model?.model)delete config.model;
    await act(`${prefix}/configure`,{version:configVersion,config});setDirty(false);message.success('测试配置已保存');
  }
  if(data.error)return <Alert type="error" message={errorText(data.error)}/>;
  return <>
    <Space direction="vertical" style={{width:'100%'}} size="middle">
      <div><Typography.Title level={4}>测试链路</Typography.Title><Typography.Paragraph type="secondary">复用用户操作步骤，验证新增能力，并保留确认过的正确结果。长链路只触发一次，随后持续观测。</Typography.Paragraph></div>
      <Alert type={value?.config.enabled?'info':'warning'} showIcon message={value?.config.enabled?'自动链路验收已启用':'自动链路验收尚未启用'} description={`${value?.driver || 'Codex + Playwright 浏览器'} · 分支测试实例 · 必要检查失败阻止验收`}/>
      <Tabs activeKey={tab} onChange={onTabChange} items={[
        {key:'test_chains',label:'链路库',children:<Space direction="vertical" style={{width:'100%'}}>
          <Space wrap><Select aria-label="生成链路的需求" placeholder="选择已确认需求" showSearch optionFilterProp="label" options={options} value={requirement} onChange={setRequirement} style={{minWidth:280}}/><Button type="primary" disabled={!requirement || busy || !value?.config.enabled} onClick={()=>perform(`${prefix}/generate`,{requirement_id:requirement})}>从需求生成链路</Button></Space>
          <Table rowKey="id" dataSource={value?.chains} loading={data.isLoading} columns={[
            {title:'链路',dataIndex:'title',render:(title:string,c:Chain)=><><strong>{title}</strong>{c.reason&&<p>{c.reason}</p>}<p className="muted">{c.requirement_ids.map(id=>reqs.find(r=>r.id===id)?.title || id.slice(0,8)).join('、')}</p></>},
            {title:'状态',dataIndex:'status',render:statusTag},
            {title:'版本 / 基准',render:(_:unknown,c:Chain)=>{const version=value?.versions.find(v=>v.id===c.current_version_id);const base=value?.baselines.find(b=>b.id===c.baseline_id);return <>{version?`v${version.number}`:'生成中'}<br/>{base?<Button type="link" onClick={()=>setSelectedRun(base.run_id)}>查看正确基准</Button>:'待确认正确结果'}</>;}},
            {title:'操作',render:(_:unknown,c:Chain)=><Space wrap><Button disabled={!c.current_version_id || busy} onClick={()=>edit(c)}>编辑 / 追加预期</Button><Button disabled={busy} onClick={()=>{linkForm.resetFields();setLinking(c);}}>复用到需求</Button><Button disabled={c.status!=='ready' || busy || !value?.config.enabled} onClick={()=>{runForm.resetFields();setRunning(c);}}>运行</Button>{['blocked','fail'].includes(c.status)&&<Button disabled={busy} onClick={()=>perform(`${prefix}/${c.id}/retry`,{version:c.version})}>重试生成</Button>}</Space>},
          ]}/>
        </Space>},
        {key:'test_chain_runs',label:'运行记录',children:<Table rowKey="id" dataSource={value?.runs.slice().reverse()} columns={[
          {title:'链路运行',dataIndex:'title',render:(title:string,r:Run)=><Button type="link" onClick={()=>setSelectedRun(r.id)}>{title}</Button>},
          {title:'结果',dataIndex:'status',render:statusTag},
          {title:'目标版本',render:(_:unknown,r:Run)=><>{r.target.branch}<br/><code>{r.target.commit?.slice(0,12)}</code></>},
          {title:'进度',render:(_:unknown,r:Run)=><>{r.current_step || r.reason || '等待调度'}<br/>操作 {r.actions || 0} 次 · 观测 {r.observations || 0} 次 · {r.tokens || 0} Token</>},
          {title:'开始时间',dataIndex:'created',render:time},
        ]}/>},
        {key:'test_chain_settings',label:'测试配置',children:<Form form={configForm} disabled={busy || data.isLoading} layout="vertical" onValuesChange={()=>setDirty(true)}>
          <Form.Item name="enabled" label="启用自动生成与验收" valuePropName="checked"><Switch/></Form.Item>
          <Form.Item name="model" label="Codex 模型" extra="留空复用项目的 Codex 验证模型。"><ReviewerSelect connection={()=>({})}/></Form.Item>
          <Space align="start" wrap>{[['interval_seconds','观测间隔（秒）',3600],['timeout_seconds','最长等待（秒）',7200],['max_observations','最大观测轮数',1000],['max_actions','最大动作数',1000]].map(([key,label,max])=><Form.Item key={key} name={key} label={label} rules={[{required:true}]}><InputNumber min={1} max={Number(max)} precision={0}/></Form.Item>)}</Space>
          <Form.Item name="origins" label="测试依赖地址" extra="每行一个 HTTP origin；测试实例本身自动允许。"><Input.TextArea rows={3} placeholder="https://test.example.com"/></Form.Item>
          <Form.Item name="storage_state" label="专用测试登录状态文件" extra="可选，填写 Playwright storageState 文件的本机绝对路径。"><Input/></Form.Item>
          <Button type="primary" loading={busy} disabled={!dirty} onClick={()=>void saveConfig().catch(()=>{})}>保存测试配置</Button>
        </Form>},
      ]}/>
    </Space>
    <Modal title="编辑链路与追加预期" open={!!editing} width={900} onCancel={()=>setEditing(undefined)} onOk={()=>void saveEdit().catch(()=>{})} confirmLoading={busy} destroyOnHidden>
      <Alert type="info" message="保存为新版本，历史正确结果始终保留。改变旧预期时，请填写替代关系。"/>
      <Form form={editForm} layout="vertical"><Form.Item name="requirement_id" label="关联变更需求" rules={[{required:true}]}><Select options={options.filter(o=>editing?.requirement_ids.includes(o.value))}/></Form.Item>
        <Form.Item name={['definition','title']} label="链路名称" rules={[{required:true}]}><Input/></Form.Item>
        <Form.List name={['definition','steps']}>{(fields,{add,remove})=><>{fields.map(field=><Card size="small" key={field.key} title={`步骤 ${field.name+1}`} extra={<Button onClick={()=>remove(field.name)}>移除</Button>} style={{marginBottom:12}}>
          <Form.Item name={[field.name,'id']} label="检查点标识" rules={[{required:true}]}><Input/></Form.Item>
          <Form.Item name={[field.name,'goal']} label="用户操作 / 目标" rules={[{required:true}]}><Input.TextArea rows={2}/></Form.Item>
          <Form.Item name={[field.name,'expected']} label="预期正确结果" rules={[{required:true}]}><Input.TextArea rows={2}/></Form.Item>
          <Form.Item name={[field.name,'loop']} valuePropName="checked"><Checkbox>执行后持续观测（Loop）</Checkbox></Form.Item>
          <Form.Item name={[field.name,'observation_action']} label="观测方式"><Select options={[{value:'none',label:'只截图等待'},{value:'reload',label:'刷新后截图（须确保不会重复提交）'},{value:'navigate',label:'打开指定进度页后截图'}]}/></Form.Item>
          <Form.Item name={[field.name,'observation_url']} label="进度页面路径" extra="仅在查看指定进度页时使用；须为测试实例内查看进度的路径。"><Input placeholder="/reports/progress"/></Form.Item>
          <Form.Item name={[field.name,'acceptance_indices']} hidden><Select mode="multiple"/></Form.Item>
        </Card>)}<Button onClick={()=>add({id:crypto.randomUUID(),goal:'',expected:'',loop:false,observation_action:'none',acceptance_indices:[]})}>追加步骤 / 预期</Button></>}</Form.List>
        <Form.List name="replacements">{(fields,{add,remove})=><><p>已有预期的替代关系（仅需要改变原预期时填写）</p>{fields.map(field=><Space key={field.key}><Form.Item name={[field.name,'old']}><Input placeholder="原检查点标识"/></Form.Item><Form.Item name={[field.name,'next']}><Input placeholder="替代检查点标识"/></Form.Item><Button onClick={()=>remove(field.name)}>移除</Button></Space>)}<Button onClick={()=>add({})}>添加替代关系</Button></>}</Form.List>
      </Form>
    </Modal>
    <Modal title="在分支实例运行" open={!!running} onCancel={()=>setRunning(undefined)} confirmLoading={busy} onOk={()=>void (async()=>{const fields=await runForm.validateFields();const r=await act(`${prefix}/${running!.id}/run`,{version:running!.version,...fields}) as Run;setRunning(undefined);setSelectedRun(r.id);})().catch(()=>{})}>
      <Form form={runForm} layout="vertical"><Form.Item name="run_id" label="分支任务与提交" rules={[{required:true}]}><Select options={tasks.data?.filter(r=>r.product_id===pid&&r.commit&&r.workspace).map(r=>({value:r.id,label:`${r.title} · ${r.branch || ''} · ${r.commit!.slice(0,8)}`}))}/></Form.Item><Form.Item name="extra_chain_ids" label="额外回归链路"><Select mode="multiple" options={value?.chains.filter(c=>c.id!==running?.id&&c.status==='ready').map(c=>({value:c.id,label:c.title}))}/></Form.Item></Form>
    </Modal>
    <Modal title="复用链路到需求" open={!!linking} onCancel={()=>setLinking(undefined)} confirmLoading={busy} onOk={()=>void (async()=>{const fields=await linkForm.validateFields();await act(`${prefix}/${linking!.id}/link`,{version:linking!.version,...fields});setLinking(undefined);})().catch(()=>{})}><Form form={linkForm} layout="vertical"><Form.Item name="requirement_id" label="复用到需求" rules={[{required:true}]}><Select options={options}/></Form.Item></Form></Modal>
    <Drawer title="运行证据与预期对照" open={!!selectedRun} onClose={()=>setSelectedRun(undefined)} width="min(1080px, 96vw)">
      {details.error&&<Alert type="error" message={errorText(details.error)}/>}
      {details.data&&<Space direction="vertical" style={{width:'100%'}} size="middle">
        <Space>{statusTag(details.data.status)}<strong>{details.data.title}</strong>{!terminal.includes(details.data.status)&&<Button danger disabled={busy} onClick={()=>perform(`${prefix}/runs/${details.data!.id}/cancel`,{version:details.data!.version})}>停止运行</Button>}{details.data.status==='pass'&&<Button type="primary" disabled={busy || details.data.baseline_confirmed} onClick={()=>perform(`${prefix}/runs/${details.data!.id}/confirm-baseline`,{version:details.data!.version})}>{details.data.baseline_confirmed?'已保存正确基准':'确认正确并保存基准'}</Button>}</Space>
        <p>{details.data.target.branch} · {details.data.target.commit}<br/>{details.data.reason || details.data.current_step}</p>
        {details.data.snapshots.map(s=><Card key={s.chain_id} size="small" title={s.definition.title}>{s.baseline_id&&value?.baselines.find(b=>b.id===s.baseline_id)&&<BaselinePreview prefix={prefix} runId={value.baselines.find(b=>b.id===s.baseline_id)!.run_id}/>} {s.definition.steps.map(step=><p key={step.id}><strong>{step.goal}</strong><br/>预期：{step.expected}{step.loop&&' · Loop'}</p>)}</Card>)}
        {details.data.evidence?.map(e=><Card key={e.id} title={<Space>{statusTag(e.status)}{e.title}</Space>} extra={time(e.at)}><p>操作：{e.action}</p><p>预期：{e.expected}</p><p>实际：{e.actual}</p>{e.screenshot&&<Screenshot id={e.id}/>}</Card>)}
      </Space>}
    </Drawer>
  </>;
}
