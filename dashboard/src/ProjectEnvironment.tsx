import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Alert, Button, Space, Table, Tag } from 'antd';
import { api } from './api';
import { ErrorNotice } from './errors';
import { time } from './components';

type Module = {path:string;stacks:string[];selection:string;evidence:string[]};
type Check = {tool:string;modules:string[];status:string;path?:string;version?:string;reason?:string;install_hint?:string};
type Environment = {stacks:string[];modules:Module[];warnings:string[];checks?:Check[];checked_at?:number;status?:string};
const labels:Record<string,string>={java:'Java 后端',react:'React 前端',vue:'Vue 前端',node:'Node.js',python:'Python',generic:'通用'};
export default function ProjectEnvironment({path}:{path:string}) {
  const detected=useQuery<Environment>({queryKey:[path,'environment'],queryFn:()=>api(`${path}/environment`),retry:false});
  const [result,setResult]=useState<Environment>();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<unknown>();
  const value=result || detected.data;
  async function check(){
    setBusy(true);setError(undefined);
    try{setResult(await api<Environment>(`${path}/runtime-check`,{}));}
    catch(e){setError(e);}
    finally{setBusy(false);}
  }
  return <section aria-label="项目技术栈与本机运行时" style={{marginBottom:24}}>
    <h4>项目技术栈与本机运行时</h4>
    <Space wrap>{value?.stacks.map(stack=><Tag key={stack}>{labels[stack] || stack}</Tag>)}
      <Button onClick={()=>void check()} loading={busy}>{result?'重新检测本机运行时':'检测本机运行时'}</Button>
      {value?.checked_at && <span className="muted">检测时间：{time(value.checked_at)}</span>}
    </Space>
    <p className="muted">检测管理服务所在电脑的已安装工具及版本。缺少工具时按提示安装，安装后重新检测；运行时可用后仍需完成项目编译、测试与业务验收。</p>
    {(error || (!result && detected.error)) && <ErrorNotice value={error || detected.error}/>}
    {value?.warnings?.map((warning,index)=><Alert key={index} type="warning" showIcon message={warning}/>)}
    {value && !value.modules.length && <Alert type="info" message="尚未识别技术栈，请在 .autopilot.json 中配置 stack 或 modules。"/>}
    {!!value?.modules.length && <Table<Module> size="small" rowKey="path" dataSource={value.modules} pagination={false} columns={[
      {title:'模块目录',dataIndex:'path'},
      {title:'识别技术栈',render:(_,m)=>m.stacks.map(s=>labels[s] || s).join(' / ')},
      {title:'识别依据',render:(_,m)=>m.evidence.join('、') || '项目显式配置'},
    ]}/>}
    {result && <Alert showIcon type={result.status==='ready'?'success':result.status==='blocked'?'warning':'info'} message={result.status==='ready'?'本机所需工具均可运行':result.status==='blocked'?'本机运行时未就绪':'未识别需要检测的工具'}/>}
    {result?.checks && <Table<Check> size="small" rowKey={c=>`${c.tool}:${c.path || c.modules.join(',')}`} dataSource={result.checks} pagination={false} columns={[
      {title:'工具',render:(_,c)=><>{c.tool}<div className="muted">{c.modules.join('、')}</div></>},
      {title:'安装状态',render:(_,c)=><Tag color={c.status==='installed'?'success':'warning'}>{({installed:'已安装且可运行',missing:'未安装或未找到',error:'安装检测失败'} as Record<string,string>)[c.status] || c.status}</Tag>},
      {title:'版本与路径',render:(_,c)=><div style={{overflowWrap:'anywhere'}}>{c.path}<pre style={{whiteSpace:'pre-wrap'}}>{c.version}</pre>{c.reason}{c.status!=='installed' && <p>{c.install_hint}</p>}</div>},
    ]}/>}
  </section>;
}
