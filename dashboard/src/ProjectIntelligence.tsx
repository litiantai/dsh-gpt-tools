import {useEffect, useRef, useState} from 'react';
import {useInfiniteQuery, useQuery} from '@tanstack/react-query';
import {useSearchParams} from 'react-router-dom';
import {App, Button, Drawer, Form, Image, Input, Select, Switch} from 'antd';
import {ArrowUpOutlined, CloseOutlined, HistoryOutlined, PaperClipOutlined, PlusOutlined, ProjectOutlined, SettingOutlined} from '@ant-design/icons';
import {api} from './api';
import {errorText} from './errors';
import {time} from './components';
import {ReviewerSelect} from './ReviewerSelect';
import './intelligence.css';

type Row = {id:string;product_id:string;version:number;status:string;created:number;updated:number;[key:string]:unknown};
type Page = {items:Row[];next_cursor:string|null};
const base=(id:string)=>`/products/${id}/intelligence`;
const strings=(value:unknown):string[]=>Array.isArray(value)?value.map(String):[];
const label=(row:Row)=>String(row.title || row.name || row.topic || '新对话');
function useRows(pid:string,kind:string,filter:Record<string,string>={}) {
  const query=useInfiniteQuery<Page>({queryKey:['intelligence',pid,kind,filter],initialPageParam:'',
    queryFn:({pageParam})=>api(`${base(pid)}/${kind}?${new URLSearchParams({...filter,cursor:String(pageParam),limit:'50'})}`),
    getNextPageParam:page=>page.next_cursor || undefined,refetchInterval:3000,retry:false});
  return {...query,rows:query.data?.pages.flatMap(p=>p.items) || []};
}
function More({query}:{query:{hasNextPage:boolean;isFetchingNextPage:boolean;fetchNextPage:()=>unknown}}) {
  return query.hasNextPage?<Button type="text" loading={query.isFetchingNextPage} onClick={()=>void query.fetchNextPage()}>更多历史</Button>:null;
}

export function IntelligenceSettings({pid,research=false}:{pid:string;research?:boolean}) {
  type Config={enabled?:boolean;research_enabled?:boolean;collaboration_enabled?:boolean;chat?:unknown;research?:unknown};
  const query=useQuery<{config:Config;version:number;capabilities:Record<string,{reason:string}>}>({queryKey:['intelligence-settings',pid],queryFn:()=>api(`${base(pid)}/settings`),retry:false});
  const [form]=Form.useForm();const [dirty,setDirty]=useState(false),[busy,setBusy]=useState(false);const {message}=App.useApp();
  useEffect(()=>{if(query.data&&!dirty)form.setFieldsValue({enabled:true,research_enabled:false,collaboration_enabled:false,...query.data.config});},[query.data,dirty,form]);
  return <Form form={form} layout="vertical" onValuesChange={()=>setDirty(true)} onFinish={async values=>{
    const keys=research?['research','research_enabled']:['chat','enabled','collaboration_enabled'];
    const changes=Object.fromEntries(keys.filter(key=>key in values).map(key=>[key,values[key]]));
    setBusy(true);try{await api(`${base(pid)}/settings`,{version:query.data?.version,config:{...query.data?.config,...changes}});setDirty(false);await query.refetch();message.success('已保存');}catch(e){message.error(errorText(e));}finally{setBusy(false);}
  }}>
    {query.error && <p role="alert">{errorText(query.error)}</p>}
    {(research?[['research_enabled','自动研究竞品']]:[['enabled','启用对话助手'],['collaboration_enabled','Agent 异步协作']]).map(([key,text])=><Form.Item key={key} name={key} label={text} valuePropName="checked"><Switch/></Form.Item>)}
    {(research?['research'] as const:['chat'] as const).map(role=><div key={role}><Form.Item name={role} label={role==='chat'?'对话模型':'竞品分析模型'}><ReviewerSelect connection={()=>({})}/></Form.Item><Button type="link" onClick={()=>{form.setFieldValue(role,null);setDirty(true);}}>继承项目需求发现模型</Button><p className="dialogue-muted">{query.data?.capabilities[role]?.reason}</p></div>)}
    {research && <p className="dialogue-muted">自动研究：每日 02:00 分析竞品，每周一 01:00 寻找新竞品，北京时间。</p>}
    <Button type="primary" htmlType="submit" loading={busy} disabled={!dirty||!query.data}>保存设置</Button>
  </Form>;
}

/** 既有任务详情仍可查看协作证据；对话页面不展示卡片或表格。 */
export function CollaborationPanel({pid,runId}:{pid:string;runId?:string}) {
  const query=useRows(pid,'agent_messages',runId?{run_id:runId}:{});
  return <div>{query.error && <p role="alert">{errorText(query.error)}</p>}{!query.rows.length && <p className="dialogue-muted">暂无协作记录</p>}
    {query.rows.map(row=><section className="collaboration-entry" key={row.id}><strong>{label(row)}</strong><p>{String(row.content || '')}</p><p>{String(row.reply || row.reason || '等待回复')}</p><small>{time(row.updated)}</small>{row.status==='needs_human' && <p><a href={`/autopilot?project=${pid}&view=intelligence&conversation=collaboration-${row.run_id}`}>在对话中回复</a></p>}</section>)}<More query={query}/></div>;
}
export function RequirementCard({row}:{row:Row;onChanged:()=>unknown}) {
  return <div className="requirement-prose"><h3>{label(row)}</h3><p>{String(row.goal || '')}</p><p>{String(row.scope || '')}</p><ol>{strings(row.acceptance).map((text,i)=><li key={i}>{text}</li>)}</ol><p>{String(row.evidence || '')}</p><a href={`/autopilot?project=${row.product_id}&view=intelligence`}>在对话中继续</a></div>;
}

function Dialogue({pid,conversation,onCreate}:{pid:string;conversation?:string;onCreate:(id:string)=>void}) {
  const messages=useRows(pid,'chat_messages',conversation?{conversation_id:conversation}:{conversation_id:'none'});
  const [content,setContent]=useState(''),[uploads,setUploads]=useState<Row[]>([]),[busy,setBusy]=useState(false),[uploading,setUploading]=useState(false),[error,setError]=useState(''),[preview,setPreview]=useState<string>();
  const history=useRef<HTMLDivElement>(null),followBottom=useRef(true),fileInput=useRef<HTMLInputElement>(null);
  const pending=useRef<{signature:string;id:string}|undefined>(undefined);
  const createdConversation=useRef<string|undefined>(undefined);
  const createOperation=useRef(crypto.randomUUID());
  const {message}=App.useApp();
  useEffect(()=>{if(messages.hasNextPage&&!messages.isFetchingNextPage)void messages.fetchNextPage();},[messages.hasNextPage,messages.isFetchingNextPage,messages.data]);
  useEffect(()=>{if(history.current&&followBottom.current)history.current.scrollTop=history.current.scrollHeight;},[messages.rows.length]);
  const waiting=messages.rows.some(row=>['queued','running'].includes(row.status));
  const send=async()=>{
    if(busy || uploading || waiting || uploads.some(row=>row.status!=='ready') || (!content.trim()&&!uploads.length))return;
    setBusy(true);setError('');followBottom.current=true;
    try{
      const conversationId=conversation || createdConversation.current || (await api<Row>(`${base(pid)}/conversations`,{title:content.slice(0,36) || '附件需求',operation_id:createOperation.current})).id;
      createdConversation.current=conversationId;
      const last=[...messages.rows].reverse().find(row=>row.role!=='user');
      const payload={conversation_id:conversationId,content,attachment_ids:uploads.map(row=>row.id),reply_to:last?.id};
      const signature=JSON.stringify(payload);
      if(pending.current?.signature!==signature)pending.current={signature,id:crypto.randomUUID()};
      await api(`${base(pid)}/chat_messages`,{...payload,operation_id:pending.current.id});
      setContent('');setUploads([]);pending.current=undefined;
      if(!conversation)onCreate(conversationId);else await messages.refetch();
    }catch(e){setError(errorText(e));}finally{setBusy(false);}
  };
  const upload=async(files:FileList|null)=>{
    if(!files)return;
    if(files.length+uploads.length>5){setError('每条消息最多五个附件');return;}
    setUploading(true);setError('');
    try{for(const file of Array.from(files)){
      if(file.size>10*1024*1024)throw new Error('单文件上限 10 MB');
      const data=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const row=await api<Row>(`${base(pid)}/attachments`,{name:file.name,data});setUploads(old=>[...old,row]);
    }}catch(e){setError(errorText(e));}finally{setUploading(false);}
  };
  const attachment=async(id:string)=>{
    try{const row=await api<Row>(`${base(pid)}/attachments/${id}`);if(String(row.mime).startsWith('image/'))setPreview(String(row.data_url));else {const a=document.createElement('a');a.href=String(row.data_url);a.download=String(row.name);a.click();}}
    catch(e){message.error(errorText(e));}
  };
  return <div className="dialogue-body">
    <div className="dialogue-scroll" ref={history} onScroll={()=>{const el=history.current;if(el)followBottom.current=el.scrollHeight-el.scrollTop-el.clientHeight<80;}}>
      <div className="dialogue-transcript" aria-live="polite">
        {!messages.rows.length && <div className="dialogue-welcome"><h1>想做点什么？</h1><p>说说你的想法，我们一起把需求做清楚。</p></div>}
        {messages.rows.map(row=><article key={row.id} className={`dialogue-message dialogue-${row.role}`}>
          {row.role!=='user' && <span className="dialogue-author">{row.role==='system'?'项目动态':'研发助手'}</span>}
          <div className="dialogue-message-text">{String(row.content || row.reason || '')}</div>
          {strings(row.attachment_ids).map((id,index)=><button className="dialogue-file-link" key={id} onClick={()=>void attachment(id)}><PaperClipOutlined/> 附件 {index+1}</button>)}
          {row.role==='user' && row.status==='failed' && <button className="dialogue-text-button" onClick={async()=>{try{await api(`${base(pid)}/chat_messages/${row.id}/retry`,{version:row.version});await messages.refetch();}catch(e){setError(errorText(e));}}}>重试回复</button>}
          {row.snapshot_at?<small className="dialogue-source-time">依据 {time(Number(row.snapshot_at))} 的项目记录</small>:null}
        </article>)}
        {waiting && <p className="dialogue-thinking"><span/>正在处理…</p>}
        {messages.error && <p role="alert" className="dialogue-error">{errorText(messages.error)}</p>}
      </div>
    </div>
    <div className="dialogue-input-region">
      <div className="dialogue-composer">
        {uploads.length>0 && <div className="dialogue-uploads">{uploads.map(row=><div key={row.id}><PaperClipOutlined/><span>{String(row.name)}</span>{row.status!=='ready' && <span className="dialogue-error">{String(row.reason)}</span>}<button aria-label={`移除 ${row.name}`} onClick={()=>setUploads(old=>old.filter(a=>a.id!==row.id))}><CloseOutlined/></button></div>)}</div>}
        <Input.TextArea aria-label="对话内容" placeholder="输入想法，或上传截图和文档…" value={content} onChange={event=>setContent(event.target.value)} autoSize={{minRows:2,maxRows:7}} maxLength={20000}
          onPressEnter={event=>{if(!event.shiftKey&&!event.nativeEvent.isComposing&&window.innerWidth>640){event.preventDefault();void send();}}}/>
        <div className="dialogue-composer-tools"><input hidden type="file" ref={fileInput} aria-label="添加截图或文档" accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.txt,.md" multiple onChange={event=>{void upload(event.target.files);event.target.value='';}}/>
          <button aria-label="上传附件" title="截图或文档，每个文件最多 10 MB" className="dialogue-icon" disabled={uploading||busy} onClick={()=>fileInput.current?.click()}><PlusOutlined/></button>
          {uploading && <small>正在上传…</small>}
          <button aria-label="发送" title="发送" className="dialogue-send" disabled={busy||uploading||waiting||(!content.trim()&&!uploads.length)||uploads.some(row=>row.status!=='ready')} onClick={()=>void send()}><ArrowUpOutlined/></button>
        </div>
      </div>
      {error && <p role="alert" className="dialogue-error">{error}</p>}
      <p className="dialogue-footnote">需求经你确认后，才会进入研发。</p>
    </div>
    <Image style={{display:'none'}} src={preview} preview={{visible:!!preview,onVisibleChange:visible=>{if(!visible)setPreview(undefined);}}}/>
  </div>;
}

export default function ProjectIntelligence({pid,projects=[]}:{pid?:string;tab?:string;projects?:Row[]}) {
  const [params,setParams]=useSearchParams();
  const [left,setLeft]=useState(false),[right,setRight]=useState(false),[settings,setSettings]=useState(false);
  const [viewportHeight,setViewportHeight]=useState<number>();
  useEffect(()=>{const viewport=window.visualViewport;const resize=()=>setViewportHeight(viewport?.height);resize();viewport?.addEventListener('resize',resize);return()=>viewport?.removeEventListener('resize',resize);},[]);
  const selected=projects.find(row=>row.id===pid);
  const selectConversation=(id?:string)=>{setParams(previous=>{const next=new URLSearchParams(previous);next.set('view','intelligence');next.set('tab','chat');if(id)next.set('conversation',id);else next.delete('conversation');return next;});setRight(false);};
  return <section className="pure-dialogue" style={viewportHeight?{height:viewportHeight-56}:undefined}>
    <header className="dialogue-header"><button className="dialogue-header-button" aria-label="项目简介" onClick={()=>setLeft(true)}><ProjectOutlined/><span>{selected?label(selected):'项目'}</span></button><span className="dialogue-header-title">研发助手</span><button className="dialogue-header-button" aria-label="历史对话" onClick={()=>setRight(true)}><HistoryOutlined/><span>历史</span></button></header>
    {pid?<Dialogue key={`${pid}:${params.get('conversation') || 'new'}`} pid={pid} conversation={params.get('conversation') || undefined} onCreate={selectConversation}/>:<div className="dialogue-welcome"><h1>先选择一个项目</h1><p>在左侧项目简介中选择项目，开始对话。</p></div>}
    <Drawer title="项目简介" placement="left" width={340} open={left} onClose={()=>setLeft(false)} rootClassName="dialogue-drawer">
      <Select aria-label="选择项目" className="full-width" placeholder="选择项目" value={pid} options={projects.map(row=>({value:row.id,label:label(row)}))} onChange={id=>{setParams(previous=>{const next=new URLSearchParams(previous);next.set('project',id);next.delete('conversation');return next;});setLeft(false);}}/>
      <div className="project-introduction"><h2>{selected?label(selected):'尚未接入项目'}</h2><p>{String(selected?.goal || '接入项目后，可以在这里通过对话梳理需求、研究竞品和追踪研发进展。')}</p>{selected && <small>{selected.status==='paused'?'研发已暂停，对话仍可使用':selected.status==='active'?'研发按项目策略运行':'当前仅监测'}</small>}</div>
      <a className="dialogue-workbench-link" href={`/autopilot?project=${pid || ''}&view=overview`}>打开项目工作台 ↗</a>
      {pid && <button aria-label="模型与运行设置" className="dialogue-settings-link" onClick={()=>{setLeft(false);setSettings(true);}}><SettingOutlined/> 模型与运行设置</button>}
    </Drawer>
    <Drawer title="历史对话" placement="right" width={340} open={right} onClose={()=>setRight(false)} rootClassName="dialogue-drawer">
      {pid && <History pid={pid} selected={params.get('conversation') || undefined} onSelect={selectConversation}/>}
    </Drawer>
    <Drawer title="模型与运行设置" placement="left" width={400} open={settings} onClose={()=>setSettings(false)} rootClassName="dialogue-drawer">{pid&&settings&&<IntelligenceSettings pid={pid}/>}</Drawer>
  </section>;
}
function History({pid,selected,onSelect}:{pid:string;selected?:string;onSelect:(id?:string)=>void}) {
  const query=useRows(pid,'conversations');
  useEffect(()=>{if(query.hasNextPage&&!query.isFetchingNextPage)void query.fetchNextPage();},[query.hasNextPage,query.isFetchingNextPage,query.data]);
  return <><button aria-label="新对话" className="dialogue-new" onClick={()=>onSelect()}><PlusOutlined/> 新对话</button><div className="dialogue-history-list">{[...query.rows].reverse().map(row=><button key={row.id} className={selected===row.id?'selected':''} onClick={()=>onSelect(row.id)}><span>{label(row)}</span><small>{time(row.updated)}</small></button>)}</div><More query={query}/></>;
}
