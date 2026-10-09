import { ErrorNotice, errorText, serializeError } from './errors';
import { useState } from 'react';
import { Alert, Button, Card, Empty, Space, Tag } from 'antd';
import { api } from './api';
import { time, useData } from './components';
import { RawRecord, RecordValue } from './RecordDetails';

interface DailyReport {
  id:string; product_id:string; title:string; status:string; reason:string; window_start:number; cutoff:number;
  stats:Record<string,number>; audits:{run_id:string;title:string;status:string;reason?:string;summary?:string;checks?:string[]}[];
  attribution_signal_ids?:string[]; attribution_results?:{status:string;reason?:string;signal_ids:string[];attributions?:{signal_id:string;reason:string;outcome:string}[]}[];
  daily_requirements?:{id:string;title:string;classification:string;reason?:string}[];
  retrospective?:unknown; exempt_usage?:number;
}
export default function DailyReports({productId}:{productId?:string}) {
  const reports=useData<DailyReport[]>('/daily_reports');
  const products=useData<{id:string;daily_report_enabled?:boolean;nightly_attribution?:boolean}[]>('/products');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const enabled=products.data?.find(p=>p.id===productId)?.daily_report_enabled;
  const nightly=products.data?.find(p=>p.id===productId)?.nightly_attribution;
  const rows=reports.data?.filter(r=>r.product_id===productId) || [];
  const toggle=async()=>{
    setBusy(true);setError('');
    try {await api(`/products/${productId}/daily-report`,{enabled:!enabled});await products.refetch();}
    catch(e){setError(serializeError(e));}finally{setBusy(false);}
  };
  return <div className="record-receipts daily-reports">
    <Alert type="info" message="每日 19:00 · 北京时间" description={nightly?"每小时巡检采集证据，19:00 统一归因全部巡检与积压信号，生成当日需求，再进行复验和复盘。19:00 后的巡检纳入次日。晚间归因、日报及复验单独记账，不占开发额度；新增需求开发仍按每日上限排队。":"生成研发日报、复盘，并对统计时段内已验收的任务逐项重新验证。19:00 后验收的任务纳入下一次复验；离线恢复后补跑。日报与复验不占每日需求数量、开发 Token 或平台审查额度。"}/>
    <Space><Tag color={enabled?'success':'default'}>{enabled?'定时日报已开启':'定时日报未开启'}</Tag><Button loading={busy} disabled={!productId} onClick={()=>void toggle()}>{enabled?'关闭定时日报':'开启定时日报'}</Button></Space>
    {(error || reports.error || products.error) && <Alert type="error" message={<ErrorNotice value={error || reports.error || products.error}/>}/>}
    {!rows.length && <Empty description={enabled?'等待下一个 19:00；今天已过 19:00 则自动补跑':'开启后将在每日 19:00 生成日报'}/>}
    {rows.map(r=><Card key={r.id} title={r.title} extra={<Tag color={r.status==='pass'?'success':r.status==='running'?'processing':'warning'}>{{pass:'复盘与复验已完成',running:'正在归因、复验与复盘',fail:'复验发现问题',blocked:'有未完成的复验或复盘'}[r.status] || r.status}</Tag>}>
      <p className="muted">统计时段：{time(r.window_start)} — {time(r.cutoff)} · {r.exempt_usage===undefined?'用量在执行完成后汇总':`本报告独立用量 ${r.exempt_usage.toLocaleString()} Token（不扣开发额度）`}</p>
      <Space wrap>{Object.entries({requirements_found:'新需求',inspections_done:'巡检',accepted_count:'待复验任务',published_count:'客户端安装版本',online_count:'已合入主分支',delivered_count:'已交付未上线',queued_count:'生成时排队任务'}).map(([key,label])=><Tag key={key}>{label} {r.stats[key] || 0}</Tag>)}</Space>
      <div className="record-text"><ErrorNotice value={r}/></div>
      {r.attribution_signal_ids && <>
        <h4>晚间统一归因 · 当日需求 {r.daily_requirements?.length || 0} 项</h4>
        <p className="muted">本次覆盖 {r.attribution_signal_ids.length} 条巡检与信号；正常或重复问题会说明原因，未完成归因的信号保留待处理。</p>
        {r.daily_requirements?.map(item=><Card key={item.id} size="small" className="record-check" title={item.title}><RecordValue value={{classification:item.classification,reason:item.reason}}/></Card>)}
        {!r.daily_requirements?.length && <p className="muted">尚无新增需求；请查看以下归因进度及结论。</p>}
        {r.attribution_results?.map((batch,index)=><details className="record-more" key={index}><summary>第 {index+1} 批 · {batch.signal_ids.length} 条 · {batch.status==='pass'?'归因完成':'归因未完成，信号已保留'}</summary><RecordValue value={{reason:batch.reason}}/>{batch.attributions?.map(a=><p className="record-text" key={a.signal_id}><Tag>{{requirement:'形成需求',duplicate:'重复问题',no_issue:'无新问题',investigation:'待调查',environment:'环境问题'}[a.outcome] || a.outcome}</Tag><span>{a.reason}</span></p>)}</details>)}
      </>}
      <h4>逐项复验</h4>
      {r.audits.length?r.audits.map(a=><Card size="small" key={a.run_id} className="record-check" title={a.title}><RecordValue value={{status:a.status,reason:a.reason,summary:a.summary,checks:a.checks}}/></Card>):<p className="muted">{r.stats.accepted_count?'复验尚未完成，暂无通过结论。':'统计时段内没有新的已验收任务。'}</p>}
      {r.retrospective!==undefined && <><h4>日报与复盘结论</h4><RecordValue value={r.retrospective}/></>}
      <RawRecord value={r}/>
    </Card>)}
  </div>;
}
