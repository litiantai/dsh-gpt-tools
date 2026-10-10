import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Alert, App, Button, Collapse, Descriptions, Drawer, Image, Input, Space, Table, Tag } from 'antd';
import { api } from './api';
import { useData, time } from './components';
import { ErrorNotice, errorText } from './errors';

type Scan = {id:string;version:number;product_id:string;source:string;status:string;reason?:string;updated:number;
  result?:{source_commit?:string;workspace?:string;configuration?:Record<string,unknown>;checks?:Check[];business_acceptance?:string}};
type Check = {name:string;status:string;command?:string[];elapsed?:number;exit_code?:number;reason?:string;log?:string;screenshot?:string;stopped?:boolean};
const names:Record<string,string>={queued:'等待扫描',running:'扫描中',pass:'启动验证通过',blocked:'需要补充条件',fail:'验证失败'};
export default function RepositoryScans({onClose,productId,onChanged}:{onClose:()=>void;productId?:string;onChanged:()=>unknown}) {
  const [source,setSource]=useState('');const [selected,setSelected]=useState<string>();
  const [busy,setBusy]=useState(false);const {message}=App.useApp();
  // 后端能力预检必须先于 /scans 请求：旧版管理服务没有 /autopilot/capabilities 路由，
  // 会返回 404「接口不存在」，前端据此判定后端不支持扫描接口并禁用入口，
  // 从而避免向不存在的接口发起请求。新后端明确返回 scan_api=true 且协议版本为 1。
  const backend=useQuery<{scan_api?:boolean;scan_api_version?:number;features?:string[]}>({queryKey:['scan-backend'],queryFn:()=>api('/autopilot/capabilities'),retry:false});
  const backendReady=backend.data?.scan_api===true && backend.data?.scan_api_version===1;
  const backendUnsupported=backend.isError || (backend.isSuccess && !backendReady);
  // 只有确认后端支持扫描接口后才请求列表；加载中/失败时 React Query 不发起 /scans，
  // 也不受全局 refetchInterval 影响，截图查询又依赖 scans.data，随之被门控。
  const scans=useData<Scan[]>('/scans',{enabled:backendReady});
  const current=scans.data?.find(s=>s.id===selected);
  // 只读能力预检：项目必须声明通用命令适配器且具备 verify 能力，且协议版本必须为 1，
  // 否则扫描/验收无法在隔离工作区执行；不匹配时只禁用入口并给出中文原因，不削弱任何校验门槛。
  const capability=useQuery<{adapter_spec?:{version?:number;kind?:string;capabilities?:string[]}}>({queryKey:['scan-capability',productId],queryFn:()=>api(`/products/${productId}`),enabled:!!productId,retry:false});
  const spec=capability.data?.adapter_spec;
  const mismatch=!!spec && (spec.version!==1 || spec.kind!=='command' || !(spec.capabilities || []).includes('verify'));
  const mismatchReason='当前项目接入能力不是通用命令适配器（adapter_spec.kind 必须为 command 且包含 verify 能力），且协议版本必须为 1，无法用隔离工作区执行扫描与验收；请先在项目设置中修正接入能力。';
  // 加载中（含后端确认中与项目能力查询中）一律不可提交；查询失败也不可提交，
  // 避免原因未明时对旧后端或未知状态发出写请求。
  const projectFailed=!!productId && capability.isError;
  const checking=backend.isPending || (!!productId && capability.isPending);
  const blocked=mismatch || backendUnsupported || projectFailed;
  const screenshot=useQuery<{data_url:string}>({queryKey:['scan-screenshot',selected],queryFn:()=>api(`/scans/${selected}/screenshot`),enabled:!!current?.result?.checks?.some(c=>c.screenshot),retry:false});
  async function act(path:string,body:Record<string,unknown>){if(checking||blocked)return;setBusy(true);try{const result=await api<Scan>(path,body);if(!path.endsWith('/apply'))setSelected(result.id);await scans.refetch();onChanged();}catch(e){message.error(errorText(e));}finally{setBusy(false);}}
  return <Drawer title="仓库接入与启动扫描" open width={960} onClose={onClose}>
    <Space direction="vertical" style={{width:'100%'}} size="large">
      <Alert type="info" showIcon message="在独立工作区识别并验证项目" description="提供本地仓库绝对路径或 HTTPS Git URL。扫描会安装依赖、构建、测试、尝试启动并保存结果；缺少外部服务或启动配置时会记录阻塞原因。启动验证不代表业务验收通过。"/>
      {checking && <Alert type="info" showIcon message="正在确认后端扫描接口与当前项目接入能力" description="确认完成前已禁用扫描提交，避免向不支持的后端发起请求。"/>}
      {backendUnsupported && <Alert type="warning" showIcon message="当前后端不支持仓库扫描接口" description="旧版管理服务缺少 /scans 接口，已禁用扫描提交；请升级并重启管理服务后重试。"/>}
      {projectFailed && <Alert type="error" showIcon message="当前项目接入能力查询失败" description={<ErrorNotice value={capability.error} subject="管理服务"/>}/>}
      {mismatch && <Alert type="warning" showIcon message="当前项目接入能力不匹配" description={mismatchReason}/>}
      <Space.Compact style={{width:'100%'}}><Input aria-label="代码仓库地址" value={source} onChange={e=>setSource(e.target.value)} placeholder="/绝对路径/项目 或 https://…/repo.git"/><Button type="primary" loading={busy} disabled={!source.trim() || checking || blocked} onClick={()=>void act('/scans',{source:source.trim()})}>扫描新仓库</Button></Space.Compact>
      <Table<Scan> size="small" rowKey="id" dataSource={scans.data} pagination={{pageSize:8}} columns={[
        {title:'仓库',dataIndex:'source',render:(v:string,s:Scan)=><Button type="link" onClick={()=>setSelected(s.id)}>{v}</Button>},
        {title:'状态',render:(_,s)=><Tag>{names[s.status] || s.status}</Tag>},
        {title:'更新时间',render:(_,s)=>time(s.updated)},
        {title:'操作',render:(_,s)=><Button disabled={busy || checking || blocked || ['queued','running'].includes(s.status)} onClick={()=>void act(`/scans/${s.id}/retry`,{version:s.version})}>重新扫描</Button>}
      ]}/>
      {current && <><Descriptions title="扫描记录" column={1} items={[
        {key:'source',label:'源码提交',children:current.result?.source_commit || '等待采集'},
        {key:'state',label:'结果',children:names[current.status] || current.status},
        {key:'business',label:'业务验收',children:'独立于启动扫描，需进入研发验收流程'}]}/>
        {current.reason && <ErrorNotice value={current.reason}/>}
        <Table<Check> rowKey="name" size="small" dataSource={current.result?.checks} pagination={false} columns={[
          {title:'步骤',dataIndex:'name'},{title:'结果',dataIndex:'status'},
          {title:'用时',render:(_,c)=>c.elapsed===undefined?'—':`${c.elapsed.toFixed(1)} 秒`},
          {title:'退出码',dataIndex:'exit_code'},{title:'说明',dataIndex:'reason'}
        ]}/>
        {current.status==='pass' && !current.product_id && <Button loading={busy} disabled={checking || blocked} onClick={()=>void act(`/scans/${current.id}/onboard`,{version:current.version})}>登记项目并进入观察模式</Button>}
        {current.status==='pass' && productId && (!current.product_id || current.product_id===productId) && <Button loading={busy} disabled={checking || blocked} onClick={async()=>{const product=await api<{version:number}>(`/products/${productId}`);await act(`/scans/${current.id}/apply`,{version:current.version,product_id:productId,product_version:product.version});}}>应用扫描配置到当前项目</Button>}
        {screenshot.data && <Image alt="隔离实例启动截图" src={screenshot.data.data_url}/>}
        <Collapse items={[{key:'raw',label:'识别配置与完整回执',children:<pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(current.result,null,2)}</pre>}]}/>
      </>}
    </Space>
  </Drawer>;
}
