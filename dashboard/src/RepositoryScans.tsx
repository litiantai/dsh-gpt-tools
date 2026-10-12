import ProjectEnvironment from './ProjectEnvironment';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Alert, App, Button, Collapse, Descriptions, Drawer, Image, Input, Space, Table, Tag } from 'antd';
import { api } from './api';
import { time } from './components';
import { ErrorNotice, errorText } from './errors';

type Scan = {id:string;version:number;product_id:string;source:string;repository_key?:string;previous_scan_id?:string;created?:number;status:string;reason?:string;updated:number;call?:unknown;
  result?:{source_commit?:string;workspace?:string;configuration?:Record<string,unknown>;checks?:Check[];business_acceptance?:string}};
type Check = {name:string;status:string;command?:string[];elapsed?:number;exit_code?:number;reason?:string;log?:string;screenshot?:string;stopped?:boolean};
const names:Record<string,string>={queued:'等待扫描',running:'扫描中',pass:'启动验证通过',blocked:'需要补充条件',fail:'验证失败',cancelled:'已取消'};
type Repository = {key:string;scans:Scan[];current:Scan;active:Scan[]};
function repositories(scans:Scan[]):Repository[] {
  const groups=new Map<string,Scan[]>();
  for(const scan of scans){const key=scan.repository_key || scan.source;const history=groups.get(key) || [];history.push(scan);groups.set(key,history);}
  return Array.from(groups,([key,history])=>{
    history.sort((a,b)=>(b.created ?? b.updated)-(a.created ?? a.updated));
    const active=history.filter(s=>['queued','running'].includes(s.status) || s.call);
    return {key,scans:history,active,current:active.find(s=>s.status==='running') || active[0] || history[0]};
  }).sort((a,b)=>Math.max(...b.scans.map(s=>s.updated))-Math.max(...a.scans.map(s=>s.updated)));
}
export default function RepositoryScans({onClose,productId,onChanged}:{onClose:()=>void;productId?:string;onChanged:()=>unknown}) {
  const [source,setSource]=useState('');const [selected,setSelected]=useState<string>();
  const [busy,setBusy]=useState(false);const {message}=App.useApp();
  // 运行身份预检：只有后端明确声明 capabilities.repository_scans>=1 才认为支持仓库扫描 API。
  // 声明来自正在运行的代码，而不是 release marker，因此旧后端不会被新 marker 伪装成受支持。
  // 每次打开抽屉都使用独立查询键并禁用缓存，上一次会话“受支持”的结论绝不能被复用：
  // 先连新后端、关闭抽屉、切换到旧后端再打开时，必须重新核对，核对完成前不发送任何 /api/scans 请求。
  const [identityKey]=useState(()=>`runtime-identity:${crypto.randomUUID()}`);
  // 独立查询键与禁用缓存用于阻断跨会话复用；同时保留全局后台轮询（不再设置 refetchInterval:false），
  // 使运行身份“先成功后失败”也能被发现：刷新失败时立即撤销缓存能力，恢复后自动放行。
  const identity=useQuery<{capabilities?:{repository_scans?:number}}>({queryKey:[identityKey],queryFn:()=>api('/runtime-identity'),retry:false,staleTime:0,gcTime:0,refetchOnWindowFocus:false});
  const scanCapability=identity.data?.capabilities?.repository_scans;
  // 后台刷新失败时查询库会保留上一次 data，且不能假定 isSuccess 一定翻转为 false；
  // 因此必须把本次查询的失败状态作为显式撤销开关：identity.isError 是权威信号，
  // 同时兼容查询库在保留 data 时只把错误放在 error 的差异，任一信号成立即视为失败；
  // 只要出现过“先成功后失败”，缓存里的 repository_scans 就不得重新放行任何扫描写操作与 /scans 轮询；
  // 成功刷新后 error 会被重置为 null，恢复不受影响。
  const identityFailed=identity.isError || !!identity.error;
  // 只有本次会话成功取回、当前没有进行中的核对且未出现失败时才承认能力；重新核对中或查询失败一律视为未确认。
  const capabilityConfirmed=identity.isSuccess && !identity.isFetching && !identityFailed;
  const supported=capabilityConfirmed && typeof scanCapability==='number' && Number.isInteger(scanCapability) && scanCapability>=1;
  // 能力缺失（旧服务未声明）与身份查询失败是两种不同语义：
  // 前者提示“管理服务需要升级”；刷新失败但仍持有缓存身份时只提示“无法核对管理服务”，不叠加升级提示。
  const outdated=capabilityConfirmed && !supported;
  const unknown=identityFailed && !identity.data;
  const identityReason=outdated
    ? '当前管理服务未声明仓库扫描能力（/api/runtime-identity 缺少 capabilities.repository_scans=1），页面无法确认其支持 /api/scans，已停止发送扫描请求。请先升级并重启管理服务，然后重新核对。'
    : '无法确认运行身份 /api/runtime-identity 声明的仓库扫描能力，页面已停止发送扫描请求。请检查管理服务后重新核对。';
  // 兼容声明：GET /api/autopilot/capabilities 只用于可编程声明与旧客户端兼容，
  // 传输层放行必须以上面运行身份返回的 capabilities.repository_scans 为准，禁止用它做正向门控，
  // 否则旧后端会被“看起来存在”的声明误放行。
  const scans=useQuery<Scan[]>({queryKey:['/scans'],queryFn:()=>api<Scan[]>('/scans'),enabled:supported,refetchInterval:3000,retry:false});
  const current=scans.data?.find(s=>s.id===selected);
  const groups=repositories(scans.data || []);
  // 只读能力预检：项目必须声明通用命令适配器且具备 verify 能力，且协议版本必须为 1，
  // 否则扫描/验收无法在隔离工作区执行；不匹配时只禁用入口并给出中文原因，不削弱任何校验门槛。
  const capability=useQuery<{adapter_spec?:{version?:number;kind?:string;capabilities?:string[]}}>({queryKey:['scan-capability',productId],queryFn:()=>api(`/products/${productId}`),enabled:!!productId && supported,retry:false});
  const spec=capability.data?.adapter_spec;
  const mismatch=!!spec && (spec.version!==1 || spec.kind!=='command' || !(spec.capabilities || []).includes('verify'));
  const mismatchReason='当前项目接入能力不是通用命令适配器（adapter_spec.kind 必须为 command 且包含 verify 能力），且协议版本必须为 1，无法用隔离工作区执行扫描与验收；请先在项目设置中修正接入能力。';
  // 加载中（后端身份确认中或项目能力查询中）与查询失败一律不可提交：
  // 原因未明时既不请求 /scans，也不对旧后端或未知状态发起写请求。
  const projectChecking=!!productId && supported && capability.isPending;
  const projectFailed=!!productId && supported && capability.isError;
  const checking=identity.isPending || identity.isFetching || projectChecking;
  // blocked 必须显式覆盖 identity.isError（先成功后失败），即使缓存 data 仍在也要拦截扫描/重新扫描/onboard/apply。
  const blocked=!supported || identityFailed || mismatch || projectFailed;
  const screenshot=useQuery<{data_url:string}>({queryKey:['scan-screenshot',selected],queryFn:()=>api(`/scans/${selected}/screenshot`),enabled:supported && !!current?.result?.checks?.some(c=>c.screenshot),retry:false});
  async function act(path:string,body:Record<string,unknown>){
    if(checking||blocked){message.error(!supported?'请先核对管理服务的仓库扫描能力':'请先完成项目接入能力核对');return;}
    setBusy(true);try{const result=await api<Scan>(path,body);if(!path.endsWith('/apply'))setSelected(result.id);await scans.refetch();onChanged();}catch(e){message.error(errorText(e));}finally{setBusy(false);}}
  return <Drawer title="仓库接入与启动扫描" open width={960} onClose={onClose}>
    <Space direction="vertical" style={{width:'100%'}} size="large">
      <Alert type="info" showIcon message="在独立工作区识别并验证项目" description="提供本地仓库绝对路径或 HTTPS Git URL。获取源码后先识别 Java、前端及模块技术栈并检测本机工具；缺少运行时会提示安装。工具就绪后再安装项目依赖、构建、测试、尝试启动并保存结果。启动验证不代表业务验收通过。"/>
      {checking && <Alert type="info" showIcon message="正在确认后端扫描接口与当前项目接入能力" description="确认完成前已禁用扫描提交，避免向不支持的后端发起请求。"/>}
      {identityFailed && <Alert type="warning" showIcon message="无法核对管理服务" description="无法读取运行身份，扫描操作已暂停。请检查服务后重新核对。" action={<Button onClick={()=>void identity.refetch()}>重新核对</Button>}/>}
      {(outdated || unknown) && <Alert type="warning" showIcon message="管理服务需要升级" description={identityReason} action={<Button size="small" onClick={()=>void identity.refetch()}>重新核对</Button>}/>}
      {projectFailed && <Alert type="error" showIcon message="当前项目接入能力查询失败" description={<ErrorNotice value={capability.error} subject="管理服务"/>} action={<Button onClick={()=>void capability.refetch()}>重新核对</Button>}/>}
      {mismatch && <Alert type="warning" showIcon message="当前项目接入能力不匹配" description={mismatchReason}/>}
      <Space.Compact style={{width:'100%'}}><Input aria-label="代码仓库地址" value={source} onChange={e=>setSource(e.target.value)} placeholder="/绝对路径/项目 或 https://…/repo.git"/><Button type="primary" loading={busy} disabled={!source.trim() || checking || blocked} onClick={()=>void act('/scans',{source:source.trim()})}>扫描新仓库</Button></Space.Compact>
      <span>共 {groups.length} 个仓库，{scans.data?.length || 0} 次扫描。展开仓库可查看历史记录。</span>
      {scans.error && <ErrorNotice value={scans.error}/>}
      <Table<Repository> size="small" rowKey="key" dataSource={groups} loading={scans.isLoading} pagination={{pageSize:8}} columns={[
        {title:'仓库',render:(_,r)=><Button type="link" style={{whiteSpace:'normal',height:'auto',textAlign:'left',overflowWrap:'anywhere'}} onClick={()=>setSelected(r.current.id)}>{r.current.source}</Button>},
        {title:'当前状态',render:(_,r)=><Space direction="vertical" size={0}><Tag>{names[r.current.status] || r.current.status}</Tag>{r.active.length>1 && <span>进行中及排队共 {r.active.length} 次</span>}</Space>},
        {title:'扫描次数',render:(_,r)=>r.scans.length},
        {title:'更新时间',render:(_,r)=>time(Math.max(...r.scans.map(s=>s.updated)))},
        {title:'操作',render:(_,r)=><Button disabled={busy || checking || blocked || r.active.length>0} onClick={()=>void act(`/scans/${r.current.id}/retry`,{version:r.current.version})}>重新扫描</Button>}
      ]} expandable={{expandedRowRender:r=><Table<Scan> aria-label="扫描历史" size="small" rowKey="id" dataSource={r.scans} pagination={{pageSize:5,hideOnSinglePage:true}} columns={[
        {title:'创建时间',render:(_,s)=>time(s.created ?? s.updated)},
        {title:'状态',render:(_,s)=><Tag>{names[s.status] || s.status}</Tag>},
        {title:'来源',render:(_,s)=>s.previous_scan_id ? <Button type="link" disabled={!scans.data?.some(previous=>previous.id===s.previous_scan_id)} onClick={()=>setSelected(s.previous_scan_id)}>重新扫描 · 查看上次</Button> : '新建扫描'},
        {title:'操作',render:(_,s)=><Button onClick={()=>setSelected(s.id)}>查看详情</Button>}
      ]}/>}}/>
      {current && <><Descriptions title="扫描记录" column={1} items={[
        {key:'id',label:'记录编号',children:current.id},
        {key:'source',label:'源码提交',children:current.result?.source_commit || '等待采集'},
        {key:'state',label:'结果',children:names[current.status] || current.status},
        {key:'business',label:'业务验收',children:'独立于启动扫描，需进入研发验收流程'}]}/>
        <ProjectEnvironment key={`${current.id}:${!!current.result}`} path={`/scans/${current.id}`}/>
        {current.reason && <ErrorNotice value={current.reason}/>}
        <Table<Check> rowKey="name" size="small" dataSource={current.result?.checks} pagination={false} columns={[
          {title:'步骤',dataIndex:'name'},{title:'结果',dataIndex:'status'},
          {title:'用时',render:(_,c)=>c.elapsed===undefined?'—':`${c.elapsed.toFixed(1)} 秒`},
          {title:'退出码',dataIndex:'exit_code'},{title:'说明',dataIndex:'reason'}
        ]}/>
        {current.status==='pass' && !current.product_id && <Button loading={busy} disabled={checking || blocked} onClick={()=>void act(`/scans/${current.id}/onboard`,{version:current.version})}>登记项目并进入观察模式</Button>}
        {current.status==='pass' && productId && (!current.product_id || current.product_id===productId) && <Button loading={busy} disabled={checking || blocked} onClick={async()=>{if(checking||blocked){message.error(!supported?'请先核对管理服务的仓库扫描能力':'请先完成项目接入能力核对');return;}const product=await api<{version:number}>(`/products/${productId}`);await act(`/scans/${current.id}/apply`,{version:current.version,product_id:productId,product_version:product.version});}}>应用扫描配置到当前项目</Button>}
        {screenshot.data && <Image alt="隔离实例启动截图" src={screenshot.data.data_url}/>}
        <Collapse items={[{key:'raw',label:'识别配置与完整回执',children:<pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(current.result,null,2)}</pre>}]}/>
      </>}
    </Space>
  </Drawer>;
}
