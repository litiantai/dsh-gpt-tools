import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Descriptions, Form, Space, Switch, Table, Tag } from 'antd';
import { api } from './api';
import { time, useData } from './components';
import { ErrorNotice, errorText, serializeError } from './errors';
import { ReviewerSelect } from './ReviewerSelect';
import type { ReviewerSelection } from './types';
import { RecordValue, recordLabel } from './RecordDetails';

type HelpPlan = {basis:string;steps:string[];resources:string[];verification:string[];stop_conditions:string[]};
type Finding = {run_id?:string;requirement_id?:string;source_id?:string;source_kind?:string;title?:string;status:string;reason?:string;action:string};
type Decision = {id:string;status:string;created:number;reason?:string;help_plan?:HelpPlan;selection?:ReviewerSelection;requirement_id?:string;result?:{run_id?:string;requirement_id?:string;action?:string;strategy?:string};material?:unknown};
type State = {version:number;enabled:boolean;config:{enabled:boolean;human_authority?:boolean;model?:ReviewerSelection|null};model?:ReviewerSelection;instance:{status:string;reason?:string;last_check?:number;watch?:{requirements_checked:number;tasks_checked:number;abnormal_count:number;findings:Finding[]}};coordination_tokens:number;decisions:Decision[]};
const states:Record<string,string> = {idle:'监听中',running:'评估中',queued:'待评估',applied:'已处理',blocked:'受阻',stale:'证据已变化',needs_human:'等待人工',waiting:'等待资源'};
const actions:Record<string,string> = {wait:'等待',repair:'追加返修',verify:'补充验证',assist:'角色协助',reprioritize:'调整排队',human:'请求人工帮助',investigate_requirement:'调度调查',propose_requirement:'高优先级需求',execute_plan:'执行解决方案'};
export default function ProjectCoordinator({pid,settings=false}:{pid?:string;settings?:boolean}) {
  const data=useData<State>(pid?`/products/${pid}/coordinator`:'/autopilot/workbench-empty');
  const [form]=Form.useForm();
  const [dirty,setDirty]=useState(false);
  const [busy,setBusy]=useState(false);
  const [version,setVersion]=useState<number>();
  const [saveError,setSaveError]=useState('');
  const baseConfig=useRef<State['config'] | undefined>(undefined);
  const loadedProject=useRef(pid);
  const {message}=App.useApp();
  const value=data.data;
  useEffect(()=>{
    if(value && !busy && (!dirty || loadedProject.current!==pid)) {
      form.setFieldsValue(value.config);
      baseConfig.current=structuredClone(value.config);
      loadedProject.current=pid;
      setVersion(value.version);setDirty(false);setSaveError('');
    }
  },[value,pid,dirty,busy,form]);
  if(!pid)return <Alert type="info" message="请先接入项目"/>;
  const act=async(action:string)=>{
    try {
      setBusy(true);
      setSaveError('');
      const config=Object.fromEntries(Object.entries(form.getFieldsValue(true)).filter(([key,item])=>
        JSON.stringify(item)!==JSON.stringify(baseConfig.current?.[key as keyof State['config']])));
      await api(`/products/${pid}/coordinator/${action}`,{version:action==='settings'?version:value?.version,
        ...(action==='settings'?{base_config:baseConfig.current,config}:{})});
      await data.refetch();setDirty(false);message.success(action==='settings'?'协调配置已保存':'已安排重新评估');
    }catch(e){if(action==='settings')setSaveError(serializeError(e));message.error(errorText(e));}finally{setBusy(false);}
  };
  const discard=async()=>{
    try {
      setBusy(true);
      const latest=await data.refetch();
      if(latest.error)throw latest.error;
      if(latest.data){form.setFieldsValue(latest.data.config);setDirty(false);setSaveError('');}
    }catch(e){message.error(errorText(e));}finally{setBusy(false);}
  };
  return <Space direction="vertical" style={{width:'100%'}} size="middle">
    <Alert type="info" showIcon message="每项目一个协调 Agent" description={'每 5 分钟核对需求与任务状态，先记录解决方案，可组合任务调度、调查、角色协助和高优先级需求；返修通过方案评审后再开发。' + (value?.config.human_authority?'Watch 已获协调决策授权，可评估并回答技术返修问题，逐轮决定修复或协助；连续两轮验证无改善仍须人工诊断，不绕过预算、暂停、取消和正式验收。':'连续两轮无进展时转人工。')}/>
    {data.error && <Alert type="error" message={errorText(data.error)}/>}
    {saveError && <Alert type="error" showIcon message="协调设置保存失败，当前修改已保留" description={<ErrorNotice value={saveError}/>}/>}
    {value && <Descriptions column={2} items={[
      {key:'state',label:'当前状态',children:<Tag>{value.enabled?(states[value.instance.status]||value.instance.status):'已停用或项目暂停'}</Tag>},
      {key:'authority',label:'协调决策权限',children:value.config.human_authority?'Watch 代行人工协调决策':'人工确认'},
      {key:'model',label:'实际协调模型',children:value.model?.model?`${value.model.provider} / ${value.model.model}`:'待配置'},
      {key:'usage',label:'今日协调 Token（不扣项目额度）',children:value.coordination_tokens.toLocaleString()},
      {key:'checked',label:'最近检查',children:value.instance.last_check?time(value.instance.last_check):'尚未检查'},
      {key:'watch',label:'最近需求检查',children:value.instance.watch?`${value.instance.watch.requirements_checked} 条需求 · ${value.instance.watch.tasks_checked} 个未结束任务 · ${value.instance.watch.abnormal_count} 项待关注`:'尚未检查'},
      {key:'reason',label:'下一步 / 等待原因',children:value.instance.reason||'等待任务或证据变化'},
    ]}/>}
    {!!value?.instance.watch?.findings.length && <Table<Finding> size="small" rowKey={r=>r.run_id||r.requirement_id||r.source_id||''} dataSource={value.instance.watch.findings} pagination={{pageSize:5}} columns={[
      {title:'待关注需求 / 任务',render:(_,r)=>r.title||r.run_id||r.requirement_id||r.source_id},
      {title:'当前状态',dataIndex:'status',render:recordLabel},
      {title:'异常依据',dataIndex:'reason'}, {title:'处理判断',dataIndex:'action'},
    ]}/>}
    {settings && <Form component={false} form={form} layout="vertical" onValuesChange={()=>setDirty(true)} disabled={busy || !baseConfig.current}>
      <Form.Item name="enabled" label="启用项目协调" valuePropName="checked" extra="默认开启，项目自主运行时生效。"><Switch/></Form.Item>
      <Form.Item name="human_authority" label="授权 Watch 代行人工协调决策" valuePropName="checked" extra="默认关闭。开启后由 AI 评估并回答技术返修问题，先保存方案再逐轮执行；连续两轮验证无改善仍须人工诊断，不绕过预算、暂停、取消及正式验收。"><Switch/></Form.Item>
      <Form.Item name="model" label="独立协调模型" extra="不指定时沿用项目验收模型。修改只影响后续评估。"><ReviewerSelect connection={()=>({})}/></Form.Item>
      <Space wrap><Button onClick={()=>{form.setFieldValue('model',null);setDirty(true);}}>沿用验收模型</Button><Button type="primary" disabled={!value || !baseConfig.current} loading={busy} onClick={()=>void act('settings')}>保存协调设置</Button><Button disabled={!dirty || busy} onClick={()=>void discard()}>放弃协调修改</Button></Space>
    </Form>}
    {!settings && <><Button data-testid="coordinator-evaluate" disabled={!value?.enabled || busy || value.instance.status==='running'} onClick={()=>void act('evaluate')}>立即评估</Button>
      <Table<Decision> rowKey="id" dataSource={value?.decisions||[]} loading={data.isLoading} pagination={{pageSize:10}} expandable={{expandedRowRender:r=><>
        {r.help_plan && <Descriptions title="帮助方案" column={1} items={[
          {key:'basis',label:'问题依据',children:r.help_plan.basis},
          ...([['steps','处理步骤'],['resources','角色与资源'],['verification','验证方式'],['stop_conditions','停止条件']] as const).map(([key,label])=>({key,label,children:<ol>{r.help_plan![key].map((text,i)=><li key={i}>{text}</li>)}</ol>})),
        ]}/>}
        <RecordValue value={r}/>
      </>}} columns={[
        {title:'时间',dataIndex:'created',render:time},
        {title:'决定',render:(_,r)=>actions[r.result?.action||'']||'项目评估'},
        {title:'任务',render:(_,r)=>r.result?.run_id||r.requirement_id||r.result?.requirement_id||'项目'},
        {title:'状态',dataIndex:'status',render:s=><Tag>{states[s]||s}</Tag>},
        {title:'帮助方案',render:(_,r)=>r.help_plan?'已记录 · 展开查看':'—'},
        {title:'依据与下一步',dataIndex:'reason'},
      ]}/></>}
  </Space>;
}
