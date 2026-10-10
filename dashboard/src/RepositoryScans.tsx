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
  const identity=useQuery<{capabilities?:{repository_scans?:number}}>({queryKey:['runtime-identity'],queryFn:()=>api('/runtime-identity'),retry:false});
  const version=identity.data?.capabilities?.repository_scans;
  const supported=typeof version==='number' && Number.isInteger(version) && version>=1;
  const scans=useQuery<Scan[]>({queryKey:['/scans'],queryFn:()=>api('/scans'),enabled:supported,refetchInterval:3000,retry:false});
  const current=scans.data?.find(s=>s.id===selected);
  const groups=repositories(scans.data || []);
  const screenshot=useQuery<{data_url:string}>({queryKey:['scan-screenshot',selected],queryFn:()=>api(`/scans/${selected}/screenshot`),enabled:supported && !!current?.result?.checks?.some(c=>c.screenshot),retry:false});
  async function act(path:string,body:Record<string,unknown>){
    if(!supported){message.error('请先核对管理服务的仓库扫描能力');return;}
    setBusy(true);try{const result=await api<Scan>(path,body);if(!path.endsWith('/apply'))setSelected(result.id);await scans.refetch();onChanged();}catch(e){message.error(errorText(e));}finally{setBusy(false);}}
  return <Drawer title="仓库接入与启动扫描" open width={960} onClose={onClose}>
    <Space direction="vertical" style={{width:'100%'}} size="large">
      <Alert type="info" showIcon message="在独立工作区识别并验证项目" description="提供本地仓库绝对路径或 HTTPS Git URL。扫描会安装依赖、构建、测试、尝试启动并保存结果；缺少外部服务或启动配置时会记录阻塞原因。启动验证不代表业务验收通过。"/>
      {!supported && (identity.isSuccess || identity.isError) && <Alert type="warning" showIcon message={identity.isError?'无法核对管理服务':'管理服务需要升级'} description={identity.isError?'无法读取运行身份，扫描操作已暂停。请检查服务后重新核对。':'当前管理服务未声明仓库扫描能力。请升级并重启管理服务后重新核对。'} action={<Button onClick={()=>void identity.refetch()}>重新核对</Button>}/>}
      <Space.Compact style={{width:'100%'}}><Input aria-label="代码仓库地址" value={source} onChange={e=>setSource(e.target.value)} placeholder="/绝对路径/项目 或 https://…/repo.git"/><Button type="primary" loading={busy} disabled={!source.trim() || !supported} onClick={()=>void act('/scans',{source:source.trim()})}>扫描新仓库</Button></Space.Compact>
      <span>共 {groups.length} 个仓库，{scans.data?.length || 0} 次扫描。展开仓库可查看历史记录。</span>
      {scans.error && <ErrorNotice value={scans.error}/>}
      <Table<Repository> size="small" rowKey="key" dataSource={groups} loading={scans.isLoading} pagination={{pageSize:8}} columns={[
        {title:'仓库',render:(_,r)=><Button type="link" style={{whiteSpace:'normal',height:'auto',textAlign:'left',overflowWrap:'anywhere'}} onClick={()=>setSelected(r.current.id)}>{r.current.source}</Button>},
        {title:'当前状态',render:(_,r)=><Space direction="vertical" size={0}><Tag>{names[r.current.status] || r.current.status}</Tag>{r.active.length>1 && <span>进行中及排队共 {r.active.length} 次</span>}</Space>},
        {title:'扫描次数',render:(_,r)=>r.scans.length},
        {title:'更新时间',render:(_,r)=>time(Math.max(...r.scans.map(s=>s.updated)))},
        {title:'操作',render:(_,r)=><Button disabled={busy || !supported || r.active.length>0} onClick={()=>void act(`/scans/${r.current.id}/retry`,{version:r.current.version})}>重新扫描</Button>}
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
        {current.reason && <ErrorNotice value={current.reason}/>}
        <Table<Check> rowKey="name" size="small" dataSource={current.result?.checks} pagination={false} columns={[
          {title:'步骤',dataIndex:'name'},{title:'结果',dataIndex:'status'},
          {title:'用时',render:(_,c)=>c.elapsed===undefined?'—':`${c.elapsed.toFixed(1)} 秒`},
          {title:'退出码',dataIndex:'exit_code'},{title:'说明',dataIndex:'reason'}
        ]}/>
        {current.status==='pass' && !current.product_id && <Button disabled={!supported} loading={busy} onClick={()=>void act(`/scans/${current.id}/onboard`,{version:current.version})}>登记项目并进入观察模式</Button>}
        {current.status==='pass' && productId && (!current.product_id || current.product_id===productId) && <Button disabled={!supported} loading={busy} onClick={async()=>{if(!supported)return;const product=await api<{version:number}>(`/products/${productId}`);await act(`/scans/${current.id}/apply`,{version:current.version,product_id:productId,product_version:product.version});}}>应用扫描配置到当前项目</Button>}
        {screenshot.data && <Image alt="隔离实例启动截图" src={screenshot.data.data_url}/>}
        <Collapse items={[{key:'raw',label:'识别配置与完整回执',children:<pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(current.result,null,2)}</pre>}]}/>
      </>}
    </Space>
  </Drawer>;
}
