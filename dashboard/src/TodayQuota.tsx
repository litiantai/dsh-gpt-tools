import { ErrorNotice, errorText, serializeError } from './errors';
import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Form, InputNumber, Modal, Space } from 'antd';
import { api } from './api';
import { time, useData } from './components';

interface Quota {
  day:string; tokens_used:number; tokens_limit:number; tokens_default_limit:number;
  tokens_override:boolean; tokens_limit_revision:number; tokens_reset_at:number; tokens_exhausted:boolean;
}

export default function TodayQuota({productId,reload}:{productId:string;reload:()=>Promise<unknown>}) {
  const query=useData<Quota>(`/products/${productId}/automation`);
  const [draft,setDraft]=useState<Quota>();
  const [limit,setLimit]=useState<number|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const warned=useRef(new Set<string>());
  const {notification,message}=App.useApp();
  const quota=query.data;
  const open=()=>{if(quota){setDraft({...quota});setLimit(quota.tokens_limit);setError('');}};
  useEffect(()=>{
    if(!quota?.tokens_exhausted)return;
    const key=`${productId}:${quota.day}:${quota.tokens_limit}`;
    if(warned.current.has(key))return;
    warned.current.add(key);
    notification.warning({key:`quota-${productId}`,message:'今日开发 Token 额度已用尽',
      description:'后续开发调用已等待额度恢复，可通过页面上的“调整今日上限”继续处理。',duration:8});
  },[productId,quota?.day,quota?.tokens_limit,quota?.tokens_exhausted,notification]);
  const valid=limit!==null && Number.isSafeInteger(limit) && limit>=0;
  const save=async()=>{
    if(!draft || !valid)return;
    setBusy(true);setError('');
    try {
      await api(`/products/${productId}/today-token-limit`,{
        day:draft.day,revision:draft.tokens_limit_revision,tokens_limit:limit,
      });
      setDraft(undefined);
      await Promise.all([query.refetch(),reload()]);
      message.success('今日上限已更新；额度充足的任务将按调度规则自动继续');
    }catch(e){setError(serializeError(e));await query.refetch();}
    finally{setBusy(false);}
  };
  if(query.error)return <Alert type="error" showIcon message="无法读取今日额度" action={<Button onClick={()=>void query.refetch()}>重新读取</Button>}/>;
  if(!quota?.day)return null;
  const amount=(value:number)=>value===0?'不限':value.toLocaleString();
  return <div style={{marginBottom:16}}>
    {quota.tokens_exhausted?<Alert type="warning" showIcon message="今日开发 Token 额度已用尽"
      description={`已用 ${quota.tokens_used.toLocaleString()} / ${amount(quota.tokens_limit)} Token。后续开发调用等待额度恢复；提高今日上限后自动重新判断。北京时间 ${time(quota.tokens_reset_at)} 恢复默认上限。`}
      action={<Button onClick={open}>调整今日上限</Button>}/>:<Space wrap>
        <span>今日开发 Token：{quota.tokens_used.toLocaleString()} / {amount(quota.tokens_limit)}{quota.tokens_override?'（今日临时上限）':''}</span>
        <Button size="small" onClick={open}>调整今日上限</Button>
      </Space>}
    <Modal title="调整今日 Token 上限" open={!!draft} onCancel={()=>!busy && setDraft(undefined)}
      onOk={()=>void save()} confirmLoading={busy} okText="保存今日上限" cancelText="取消" okButtonProps={{disabled:!valid}} destroyOnClose>
      <p>仅对北京时间 {draft?.day} 生效，次日零点恢复默认上限 {amount(draft?.tokens_default_limit ?? 0)}。仅统计需求方案至业务验收；代码复审、交付修复及复验独立记账。</p>
      <p>今日已用 {quota.tokens_used.toLocaleString()} Token；输入的是今日总上限。</p>
      <Form layout="vertical"><Form.Item label="今日 Token 总上限" htmlFor="today-token-limit" extra="0 表示今日不限；不会清空已用额度。">
        <InputNumber id="today-token-limit" aria-label="今日 Token 总上限" min={0} max={Number.MAX_SAFE_INTEGER} precision={0}
          value={limit} onChange={setLimit} disabled={busy} style={{width:'100%'}} addonAfter="Token"/>
      </Form.Item></Form>
      {limit!==null && limit>0 && limit<=quota.tokens_used && <Alert type="warning" showIcon message="此上限不高于今日已用量，保存后仍会等待额度恢复。"/>}
      {error && <Alert type="error" showIcon message={<ErrorNotice value={error}/>} description="草稿已保留；日期或额度发生变化时，请关闭后重新打开，核对最新额度。"/>}
    </Modal>
  </div>;
}
