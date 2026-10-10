import { ErrorNotice, errorText, serializeError } from './errors';
import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, ConfigProvider, Descriptions, Form, Input, InputNumber, Select, Space, Switch, Table, Tabs, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { api } from './api';
import { useData, time } from './components';
import { ReviewerSelect } from './ReviewerSelect';
import ProjectCoordinator from './ProjectCoordinator';
import { IntelligenceSettings } from './ProjectIntelligence';

interface Project {id:string;version:number;name?:string;goal?:string;source?:string;status:string;[key:string]:unknown}
const policyFields = [
  {key:'probe_seconds',label:'运行监测间隔',unit:'分钟',factor:60},
  {key:'inspection_seconds',label:'体验巡查间隔',unit:'小时',factor:3600},
  {key:'runs_per_day',label:'每日需求处理上限',unit:'个',factor:1},
  {key:'tokens_per_day',label:'每日 Token 上限',unit:'Token',factor:1},
  {key:'discovery_per_day',label:'每天需求发现调用',unit:'次',factor:1},
  {key:'execution_seconds',label:'每任务累计执行上限',unit:'分钟',factor:60},
  {key:'max_revisions',label:'每任务最多返修',unit:'次',factor:1},
  {key:'idle_seconds',label:'发布前连续空闲',unit:'分钟',factor:60},
  {key:'observation_seconds',label:'发布后观察',unit:'分钟',factor:60},
];
export default function ProjectSettings({project,reload,tab,onTabChange,hasRunningTasks}:{project?:Project;reload:()=>Promise<unknown>;tab:string;onTabChange:(key:string)=>void;hasRunningTasks:boolean}) {
  const [form]=Form.useForm();
  const gitEnabled=Form.useWatch(['git','enabled'],form);
  const [busy,setBusy]=useState(false);
  const [dirty,setDirty]=useState(false);
  const [saveError,setSaveError]=useState('');
  const loadedVersion=useRef(project?.version);
  const baseConfig=useRef<Record<string,unknown>>({});
  const {message}=App.useApp();
  const automation=useData<{tokens_used:number;code_delivery_tokens_used?:number;tasks_used:number;tokens_limit:number;tasks_limit:number;queued:number;next_inspection:number;deepseek_schedule?:{enabled:boolean;peak:boolean;next_allowed_at:number|null;holiday_calendar_known:boolean;holiday_calendar_year:number}}>(project?`/products/${project.id}/automation`:'/autopilot/workbench-empty');
  const runs=useData<{id:string;status:string}[]>('/runs');
  useEffect(()=>{
    if(project && !dirty) {
      const roles=project.agents as Record<string,unknown> | undefined;
      form.setFieldsValue({runtime_recovery:project.runtime_recovery || {enabled:false},goal:project.goal,agents:project.agents,policy:{tokens_per_day:0,deepseek_off_peak_only:true,...project.policy as object},
        git:project.git || {url:'',base_branch:'master',enabled:false},
        code_review:{reviewer:roles?.acceptance,fixer:roles?.implementation,max_revisions:0,...project.code_review as object}});
      loadedVersion.current=project.version;
      baseConfig.current=structuredClone({runtime_recovery:project.runtime_recovery ?? null,goal:project.goal ?? null,agents:project.agents ?? null,policy:project.policy ?? null,git:project.git ?? null,code_review:project.code_review ?? null});
      setSaveError('');
    }
  },[project?.version,project?.id,dirty,form]);
  if(!project)return <Alert type="info" message="请先接入项目"/>;
  const gitLocked=hasRunningTasks && (gitEnabled || (project.git as {enabled?:boolean} | undefined)?.enabled);
  const testEnvironment=project.test_environment as {application?:string;commit?:string;origin?:string;run_id?:string;status?:string} | undefined;
  const masterEnvironment=project.master_environment as {commit?:string;origin?:string} | undefined;
  const previewRun=runs.data?.find(run=>run.id===testEnvironment?.run_id);
  const save=async()=>{
    try {
      await form.validateFields();
      const config=form.getFieldsValue(true);
      if(!config.git?.url?.trim() && !config.git?.enabled)delete config.git;
      if(!config.code_review?.reviewer?.model && !config.code_review?.fixer?.model)delete config.code_review;
      setBusy(true);
      setSaveError('');
      await api(`/products/${project.id}/configure`,{version:loadedVersion.current,base_config:baseConfig.current,config});
      await reload();setDirty(false);message.success('项目配置已保存');
    } catch(e){
      const reason=serializeError(e);
      setSaveError(reason);message.error(errorText(reason));
      await reload();
    }finally{setBusy(false);}
  };
  const mode=async(action:string)=>{
    try{setBusy(true);await api(`/products/${project.id}/${action}`,{version:project.version});await reload();message.success(action==='recover-runtime'?'已安排恢复应用':'运行模式已更新');}
    catch(e){message.error(errorText(e));}finally{setBusy(false);}
  };
  return <>
    <div className="project-mode-bar">
      <div><strong>项目运行模式</strong><Tag color={project.status==='active'?'processing':'default'}>{project.status==='active'?'自主运行':project.status==='observing'?'仅监测':'已暂停'}</Tag><p>仅监测会持续采集信号；自主运行会按策略推进开发与发布。已有任务的暂停在任务流水线操作。</p></div>
      <Space wrap><Button disabled={project.status==='observing' || busy} onClick={()=>void mode('observe')}>仅监测</Button><Button disabled={busy} onClick={()=>void mode(project.status==='active'?'pause':'enable')}>{project.status==='active'?'暂停自主运行':'启用自主运行'}</Button></Space>
    </div>
    {!!project.evaluation_id && <Alert type="warning" showIcon message="当前仍受评测批次数量限制" description="完成评测后切换持续运行，后续需求按每日额度排队，验收通过后进入发布。" action={<Button disabled={busy} onClick={()=>void mode('continuous')}>切换持续运行</Button>}/>}
    {project.nightly_attribution===true && <Alert type="info" message="每晚统一归因" description="白天按巡检间隔采集证据；每天北京时间 19:00 全部归因并生成当日需求。需求发现调用次数限制不适用于晚间归因，开发仍遵守每日需求和 Token 上限。"/>}
    {automation.data && <p className="muted">今日需求 {automation.data.tasks_used} / {automation.data.tasks_limit || '不限'} · 今日 Token {automation.data.tokens_used.toLocaleString()} / {automation.data.tokens_limit || '不限'} · 排队 {automation.data.queued} 项 · 下次巡检 {automation.data.next_inspection<Date.now()/1000?'等待调度':time(automation.data.next_inspection)}</p>}
    {automation.data && <p className="muted">今日代码交付评审、修复及复验：{(automation.data.code_delivery_tokens_used || 0).toLocaleString()} Token，单独记账，不占开发每日额度。</p>}
    {hasRunningTasks && <Alert type="info" showIcon message="任务执行中，可调整额度、DeepSeek 时段开关及代码评审模型" description="配置仅影响后续调用；开发模型及其他执行策略在任务结束后修改。"/>}
    {saveError && <Alert type="error" showIcon message="保存失败，当前修改已保留" description={<ErrorNotice value={saveError}/>}/>}
    <Form form={form} layout="vertical" disabled={busy} onValuesChange={()=>setDirty(true)}>
      <Tabs activeKey={tab} onChange={onTabChange} items={[
        {key:'coordination_settings',label:'项目协调',children:<ProjectCoordinator pid={project.id} settings/>},
        {key:'research',label:'竞品分析配置',children:null},
        {key:'git',label:'Git 与代码评审',forceRender:true,children:<>
          <Alert type="info" showIcon message="合入 master 才标记已上线" description="release 每天从 master 创建。feat → release MR 必须先通过 Code Review；23:30 封板并停止新评审，统一验证后再合入 master。合入后等待主实例空闲更新，再在 master 实例进行最终复验。"/>
          <div className="project-settings-grid">
            <Form.Item name={['git','url']} label="GitHub 仓库地址"><Input placeholder="https://github.com/owner/repository" disabled={gitLocked}/></Form.Item>
            <Form.Item name={['git','base_branch']} label="目标分支"><Input disabled={gitLocked}/></Form.Item>
            <Form.Item name={['git','enabled']} label="启用每日 Git 交付" valuePropName="checked"><Switch disabled={hasRunningTasks}/></Form.Item>
            <Form.Item name={['code_review','max_revisions']} label="每批次每日自动修复轮数" extra="0 表示不限；用尽后修复排队至北京时间次日零点，已通过评审与验证的成果继续合入主分支。" rules={[{type:'number',min:0,max:20}]}><InputNumber min={0} max={20} precision={0}/></Form.Item>
          </div>
          <GitHubTokenSettings productId={project.id} configuredRepository={!!(project.git as {url?:string})?.url} reload={reload}/>
          <div className="project-settings-grid">{[['reviewer','代码评审模型'],['fixer','问题修复模型']].map(([key,label])=><div key={key}>
            <Form.Item name={['code_review',key]} label={label}><ReviewerSelect connection={()=>({})}/></Form.Item>
            <Form.Item name={['code_review',key,'reasoning_effort']} label="推理强度（Codex）"><Select allowClear placeholder="medium" options={['low','medium','high','xhigh','max','ultra'].map(value=>({value,label:value}))}/></Form.Item>
          </div>)}</div>
          <p className="muted">模型列表来自所选执行器；目录不可用时保留已保存选择。新配置只影响后续轮次，历史记录保留实际使用模型。</p>
          <Descriptions column={1} items={[{key:'migration',label:'源码基线迁移',children:String((project.git_migration as {status?:string} | undefined)?.status || '未开始')}]} />
          <Button disabled={dirty || busy || hasRunningTasks || !(project.git as {enabled?:boolean} | undefined)?.enabled || (project.git_migration as {status?:string} | undefined)?.status==='completed'} onClick={()=>void mode('migrate-git')}>建立首次基线 PR</Button>
        </>},
        {key:'roles',label:'模型分工',forceRender:true,children:<>
          <Alert type="info" showIcon message="按角色选择执行器与模型" description="验证先执行项目的必需检查，再由独立模型核验；配置只影响后续调用，不自动替换不可用的模型。"/>
          <ConfigProvider componentDisabled={busy || hasRunningTasks}><div className="project-settings-grid">{[
            ['discovery','需求发现','codex'],['implementation','方案与实现','harness'],['verification','独立验证','codex'],['acceptance','方案审查与业务验收','codex'],
          ].map(([key,label])=><Form.Item key={key} name={['agents',key]} label={label} rules={[{validator:(_,value)=>value?.provider && value?.model?.trim()?Promise.resolve():Promise.reject(new Error('请选择执行器和模型'))}]}><ReviewerSelect connection={()=>({})}/></Form.Item>)}</div></ConfigProvider>
        </>},
        {key:'policy',label:'运行策略',forceRender:true,children:<>
          <Form.Item name={['policy','deepseek_off_peak_only']} label="DeepSeek 仅空闲时段运行" valuePropName="checked" extra="北京时间周一至周五（不含中国法定节假日）09:00–12:00、14:00–18:00 为高峰。开启后，本项目 DeepSeek 官方调用在高峰期排队，空闲时段自动继续；周末及节假日全天放行。已启动的执行任务允许完成，等待不计执行超时或返修次数。"><Switch/></Form.Item>
          {automation.data?.deepseek_schedule?.enabled && <Alert type="info" showIcon message={automation.data.deepseek_schedule.peak?'DeepSeek 当前为高峰时段':'DeepSeek 当前为空闲时段'} description={automation.data.deepseek_schedule.next_allowed_at?`新 DeepSeek 任务等待至北京时间 ${new Date(automation.data.deepseek_schedule.next_allowed_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})} 后自动继续；关闭开关可解除时段等待。`:'DeepSeek 任务可按队列及额度正常执行。'}/>}
          {automation.data?.deepseek_schedule && !automation.data.deepseek_schedule.holiday_calendar_known && <Alert type="warning" showIcon message={`${automation.data.deepseek_schedule.holiday_calendar_year} 年节假日表尚未更新`} description="暂按周一至周五的高峰时间排队，周末仍全天放行；更新日历后才能识别该年的节假日。"/>}
          <Form.Item name={['runtime_recovery','enabled']} label="应用自动恢复" valuePropName="checked" extra="开启后，正常退出应用也会自动重新启动；暂停项目后停止恢复。连续失败会延迟重试。"><Switch/></Form.Item>
          <Button disabled={busy || dirty || !(project.runtime_recovery as {enabled?:boolean} | undefined)?.enabled || project.status==='paused'} onClick={()=>void mode('recover-runtime')}>立即尝试恢复</Button>
          <Form.Item name="goal" label="项目目标与范围" rules={[{required:true}]}><Input.TextArea rows={3} disabled={busy || hasRunningTasks}/></Form.Item>
          <div className="project-settings-grid">{policyFields.map(({key,label,unit,factor})=>{const quota=['runs_per_day','discovery_per_day','tokens_per_day'].includes(key);return <Form.Item key={key} name={['policy',key]} label={label} extra={quota?'0 表示不限；超限等待北京时间次日零点':' '}
            getValueProps={value=>({value:typeof value==='number'?value/factor:undefined})}
            normalize={value=>typeof value==='number'?Math.round(value*factor):value}
            rules={[{required:true,message:'请填写额度或间隔'},{type:'number',min:quota?0:1,message:quota?'不能小于 0':'必须大于 0'}]}>
            <InputNumber min={quota?0:1/factor} step={1} precision={factor===1?0:undefined} addonAfter={unit} style={{width:'100%'}} disabled={busy || (hasRunningTasks && !quota)}/>
          </Form.Item>})}</div>
          <p className="muted">每日 Token 额度仅统计需求方案、方案审查、开发、业务验证和业务验收，含缓存输入。巡检不占用需求与 Token 额度；需求发现、前期调查单独记账，不占开发 Token 额度。代码交付评审、修复和复验单独记账，不占此额度。当前调用完成后可能超过上限，后续开发调用等待额度恢复。平台审查额度仍独立生效。</p>
        </>},
        {key:'notifications',label:'通知',forceRender:true,children:<NotificationSettings productId={project.id} reload={reload}/>},
        {key:'environment',label:'接入环境',children:<>
          <Descriptions column={1} items={['source','repository','application','worker_runtime'].map(key=>({key,label:({source:'原始源码',repository:'受管源码快照',application:'正式应用',worker_runtime:'固定执行运行时'} as Record<string,string>)[key],children:String(project[key] || '尚未配置')}))}/>
          <Alert type="info" message={project.test_environment?'独立测试环境已登记':'尚未登记独立测试环境'} description="巡检与合入后的最终复验共用 master 主实例；开发和业务验收共用一个独立测试实例。主实例只执行只读检查，登录、发送会话与生成报告在测试实例完成。"/>
          {!!(project.git as {enabled?:boolean})?.enabled && <Descriptions column={1} items={[
            {key:'masterCommit',label:'master 主实例版本',children:masterEnvironment?.commit || '等待主实例更新并核对版本'},
            {key:'masterOrigin',label:'master Host 地址',children:masterEnvironment?.origin || '尚未核对'},
          ]}/>}
          {previewRun?.status==='rolled_back' && <Alert type="warning" showIcon message="当前测试入口来自已回滚任务" description="这个测试版曾通过业务验收，但其发布已回滚；不能据此判定代码或正式客户端已上线。"/>}
          {testEnvironment && <Descriptions column={1} items={[
            {key:'testApp',label:'当前测试应用',children:testEnvironment.application || '未记录'},
            {key:'testCommit',label:'测试应用源码版本',children:testEnvironment.commit || '旧测试基线（未关联已验收版本）'},
            {key:'testOrigin',label:'测试 Host 地址',children:testEnvironment.origin || '未记录'},
            {key:'testStatus',label:'测试实例状态',children:({ready:'运行中',starting:'启动中',stopped:'已停止，验收时按需启动'} as Record<string,string>)[testEnvironment.status || ''] || '尚未核对'},
          ]}/>}
        </>},
      ]}/>
      {tab!=='research' && <div className="project-settings-actions"><Space wrap><Button aria-label="保存项目设置" type="primary" onClick={()=>void save()} loading={busy} disabled={!dirty}>保存项目设置</Button><Button disabled={!dirty || busy} onClick={()=>setDirty(false)}>放弃修改</Button><span className="muted">{dirty?'有未保存的修改，切换上方配置标签会保留草稿':'修改后保存，仅对当前项目生效'}</span></Space></div>}
    </Form>
    {tab==='research' && <IntelligenceSettings key={project.id} pid={project.id} research/>}
  </>;
}

function GitHubTokenSettings({productId,configuredRepository,reload}:{productId:string;configuredRepository:boolean;reload:()=>Promise<unknown>}) {
  const saved=useData<{configured:boolean}>(`/products/${productId}/git-token`);
  const [token,setToken]=useState('');
  const [saving,setSaving]=useState(false);
  const [error,setError]=useState('');
  const {message}=App.useApp();
  const save=async()=>{
    setSaving(true);setError('');
    try {
      await api(`/products/${productId}/git-token`,{token:token.trim()});
      setToken('');
      await saved.refetch();await reload();
      message.success('GitHub Token 已验证并保存到本机');
    }catch(e){setError(e instanceof Error?e.message:'Token 保存失败');}
    finally{setSaving(false);}
  };
  return <div style={{marginBottom:24}}>
    <strong>本机 GitHub Token</strong>
    <p className="muted">{saved.data?.configured?'已保存 Token；输入新值可替换。':'可直接配置 Personal Access Token。'}本机所有项目共用，优先于环境变量与 GitHub CLI；仅当前系统用户可读取，不进入项目配置、操作日志或页面回显。</p>
    {!configuredRepository && <Alert type="info" message="请先保存 GitHub 仓库地址，再验证 Token"/>}
    {(error || saved.error) && <Alert type="error" showIcon message={error || '无法读取本机 Token 配置状态'}/>}
    <Space.Compact style={{width:'100%',maxWidth:650}}>
      <Input.Password aria-label="本机 GitHub Token" placeholder="输入 GitHub Personal Access Token" autoComplete="off" visibilityToggle={false} value={token} onChange={e=>setToken(e.target.value)} disabled={saving}/>
      <Button aria-label="验证并保存 Token" onClick={()=>void save()} loading={saving} disabled={!configuredRepository || !token.trim()}>验证并保存 Token</Button>
    </Space.Compact>
  </div>;
}

interface NotificationDelivery {id:string;kind:string;subject:string;status:string;created:number;attempts:number;error?:string;receipt?:{message_id?:string}}
interface NotificationView {
  enabled:boolean;sender_masked:string;sender_configured:boolean;credential_configured:boolean;
  recipients:string[];events:Record<string,boolean>;
  smtp:{host:string;port:number;tls:string};morning_hour:number;
  recent:NotificationDelivery[];stats:Record<string,number>;
}
const notificationEvents:[string,string][] = [
  ['run_report','运行节点生成报告'],
  ['alert','告警'],
  ['quota_exhausted','额度用尽提示'],
  ['competitor','竞品发现-分析'],
  ['morning_retro','每日早上七点给出复盘报告'],
];
const notificationStatus:Record<string,string> = {sent:'SMTP 已接受',partial:'部分发送失败',queued:'待发送',failed:'发送失败',blocked:'已阻塞'};
function NotificationSettings({productId,reload}:{productId:string;reload:()=>Promise<unknown>}) {
  const saved=useData<NotificationView>(`/products/${productId}/notifications`);
  const [enabled,setEnabled]=useState(false);
  const [sender,setSender]=useState('');
  const [smtp,setSmtp]=useState({host:'smtp.qq.com',port:465,tls:'ssl'});
  const [events,setEvents]=useState<Record<string,boolean>>({});
  const [morningHour,setMorningHour]=useState(7);
  const [recipients,setRecipients]=useState<string[]>([]);
  const [recipient,setRecipient]=useState('');
  const [credential,setCredential]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const {message}=App.useApp();
  useEffect(()=>{
    if(!saved.data)return;
    setEnabled(saved.data.enabled);
    setSender('');
    setSmtp(saved.data.smtp);
    setEvents(saved.data.events);
    setMorningHour(saved.data.morning_hour);
    setRecipients(saved.data.recipients);
  },[saved.data?.sender_masked,saved.data?.credential_configured,saved.data?.enabled]);
  const addRecipient=()=>{
    const value=recipient.trim();
    if(!value||recipients.includes(value)){setRecipient('');return;}
    setRecipients([...recipients,value]);setRecipient('');
  };
  const save=async()=>{
    setBusy(true);setError('');
    try{
      const config:Record<string,unknown>={enabled,smtp,events,morning_hour:morningHour,recipients};
      if(sender.trim())config.sender=sender.trim();
      const body:Record<string,unknown>={config};
      if(credential.trim())body.credential=credential.trim();
      await api(`/products/${productId}/notifications/configure`,body);
      setCredential('');await saved.refetch();await reload();
      message.success('邮件通知设置已保存');
    }catch(e){setError(e instanceof Error?e.message:'通知设置保存失败');}
    finally{setBusy(false);}
  };
  const sendTest=async()=>{
    setBusy(true);setError('');
    try{await api(`/products/${productId}/notifications/send-test`,{});await saved.refetch();message.success('测试简报已入队，将在下一轮调度发送');}
    catch(e){setError(e instanceof Error?e.message:'测试简报入队失败');}
    finally{setBusy(false);}
  };
  const columns:TableColumnsType<NotificationDelivery>=[
    {title:'类型',width:120,render:(_,row)=>row.kind},
    {title:'主题',render:(_,row)=>row.subject},
    {title:'状态',width:110,render:(_,row)=><Tag color={row.status==='sent'?'success':['failed','partial'].includes(row.status)?'error':row.status==='blocked'?'warning':'default'}>{notificationStatus[row.status] || row.status}</Tag>},
    {title:'时间',width:160,render:(_,row)=>time(row.created)},
    {title:'结果',width:240,render:(_,row)=><span className="muted">{row.error || row.receipt?.message_id || '—'}</span>},
  ];
  return <div>
    <Alert type="info" showIcon message="邮件在本机直接发送" description="授权码只保存在本机运行时目录，不进入项目配置、操作日志或页面回显；SMTP 接受表示服务器已接收邮件，实际送达请以收件箱为准。"/>
    {(error || saved.error) && <Alert type="error" showIcon message={error || '无法读取当前通知配置'}/>}
    <div className="project-settings-grid">
      <div><label className="muted">发件人邮箱</label><Input aria-label="发件人邮箱" placeholder={saved.data?.sender_configured?saved.data.sender_masked:'发件人邮箱'} autoComplete="off" value={sender} onChange={e=>setSender(e.target.value)} disabled={busy}/>
        <p className="muted">当前：{saved.data?.sender_configured?saved.data.sender_masked:'尚未配置'}；输入新值可替换。</p></div>
      <div><label className="muted">邮箱授权码</label><Input.Password aria-label="邮箱授权码" placeholder={saved.data?.credential_configured?'已配置，输入新值可替换':'输入 SMTP 授权码'} autoComplete="off" visibilityToggle={false} value={credential} onChange={e=>setCredential(e.target.value)} disabled={busy}/>
        <p className="muted">{saved.data?.credential_configured?'本机已保存授权码，保存时会重新验证登录。':'只保存到本机，不回显、不进入台账。'}</p></div>
      <div><label className="muted">SMTP 服务器</label><Input aria-label="SMTP 服务器" value={smtp.host} onChange={e=>setSmtp({...smtp,host:e.target.value})} disabled={busy}/></div>
      <div><label className="muted">SMTP 端口</label><InputNumber aria-label="SMTP 端口" min={1} max={65535} precision={0} style={{width:'100%'}} value={smtp.port} onChange={value=>setSmtp({...smtp,port:typeof value==='number'?value:465})} disabled={busy}/></div>
      <div><label className="muted">加密方式</label><Select aria-label="加密方式" style={{width:'100%'}} value={smtp.tls} onChange={value=>setSmtp({...smtp,tls:value})} disabled={busy} options={[{value:'ssl',label:'SSL（465）'},{value:'starttls',label:'STARTTLS（587）'},{value:'none',label:'不加密'}]}/></div>
      <div><label className="muted">每日复盘时间（北京时间整点）</label><InputNumber aria-label="每日复盘时间" min={0} max={23} precision={0} style={{width:'100%'}} value={morningHour} onChange={value=>setMorningHour(typeof value==='number'?value:7)} disabled={busy}/></div>
    </div>
    <Switch aria-label="启用邮件通知" checked={enabled} onChange={setEnabled} disabled={busy}/> <span>启用邮件通知</span>
    <div style={{marginTop:16}}>
      <strong>收件人邮箱列表</strong>
      <p className="muted">可持续新增；保存后生效，重复地址自动去重。</p>
      <Space wrap>{recipients.map(value=><Tag key={value} closable onClose={()=>setRecipients(recipients.filter(item=>item!==value))}>{value}</Tag>)}</Space>
      <Space.Compact style={{width:'100%',maxWidth:650,display:'flex',marginTop:8}}>
        <Input aria-label="新增收件人" placeholder="name@example.com" value={recipient} onChange={e=>setRecipient(e.target.value)} onPressEnter={()=>addRecipient()} disabled={busy}/>
        <Button aria-label="添加收件人" onClick={()=>addRecipient()} disabled={busy||!recipient.trim()}>添加</Button>
      </Space.Compact>
    </div>
    <div style={{marginTop:16}}>
      <strong>通知事件</strong>
      <div className="project-settings-grid">{notificationEvents.map(([key,label])=><label key={key} className="muted"><Switch aria-label={label} checked={!!events[key]} onChange={value=>setEvents({...events,[key]:value})} disabled={busy}/> {label}</label>)}</div>
    </div>
    <Space wrap style={{marginTop:16}}>
      <Button aria-label="保存通知设置" type="primary" onClick={()=>void save()} loading={busy}>保存通知设置</Button>
      <Button aria-label="发送测试简报" onClick={()=>void sendTest()} loading={busy} disabled={busy||!saved.data?.sender_configured||!saved.data?.credential_configured||recipients.length===0}>发送测试简报</Button>
    </Space>
    <h4 style={{marginTop:20}}>最近投递</h4>
    <p className="muted">SMTP 已接受 {saved.data?.stats.sent || 0} · 部分失败 {saved.data?.stats.partial || 0} · 待发送 {saved.data?.stats.queued || 0} · 失败 {saved.data?.stats.failed || 0} · 阻塞 {saved.data?.stats.blocked || 0}</p>
    <Table<NotificationDelivery> rowKey="id" size="small" dataSource={saved.data?.recent || []} columns={columns} pagination={{pageSize:5,showSizeChanger:false}}/>
  </div>;
}
