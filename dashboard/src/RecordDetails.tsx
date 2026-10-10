import { ErrorNotice, TechnicalDetails, errorText } from './errors';
import { Card, Descriptions, Empty, Space, Tag } from 'antd';
import { eventLabels, labels, time } from './components';

export type RecordData = Record<string, unknown>;
const names: Record<string, string> = {
  pending_confirmation:'待确认',waiting_for_reply:'等待协作回复',rejected:'已拒绝',
  next_auto_retry_at:'下次异常重试',last_auto_retry_at:'最近自动重试',auto_retry_count:'异常自动重试次数',auto_retry_wait_reason:'重试等待原因',last_retry_reason:'上次异常原因',
  repair_deferred_until:'修复额度恢复时间',failure_kind:'异常类型',excluded_repair_rounds:'不计入修复额度的异常轮次',
  adapter_exit_code:'回执程序退出码',report_url:'GitHub 评审报告',
  path:'问题文件',start_line:'起始行',reporting_error:'GitHub 状态回写异常',
  feature_pr_url:'代码评审 MR',feature_prs:'已合入 release 的评审记录',feature_head_sha:'feat 提交',feature_base_sha:'release 评审基线',feature_review_pass:'代码评审通过证据',validation_pass:'统一验证证据',cutoff:'封板时间',deferred_ids:'移至次日的任务',selection:'实际模型配置',head_sha:'评审提交',base_sha:'目标提交',merge_sha:'合并提交',skill_version:'技能版本',coverage:'文件覆盖',severity:'严重程度',pr_url:'GitHub PR',branch:'交付分支',
  accomplishments:'完成事项',problems:'问题与原因',lessons:'复盘结论',next_actions:'后续行动',
  daily_acceptance:'晚间复验',daily_retrospective:'日报复盘',
  investigation_result:'调查结论',next_investigation:'下次调查时间',review_retries:'审查自动重试次数',
  outcome:'归因结论',requirement_day:'需求归属日期',attribution:'归因说明',
  status:'结果',decision:'审查结论',reason:'原因说明',summary:'结果摘要',instruction:'下一步指令',
  title:'名称',name:'名称',message:'说明',error:'错误说明',detail:'详细说明',details:'详细记录',
  action:'执行步骤',phase:'阶段',expected:'预期结果',actual:'实际结果',judgement:'判断依据',
  evidence:'依据',reproduction:'复现步骤',impact:'用户影响',acceptance:'验收条件',
  requirement:'需求内容',requirements:'发现的需求',plan:'开发方案',feedback:'返修要求',
  checks:'检查项目',issues:'发现的问题',execution:'执行情况',result:'执行结果',data:'事件内容',
  text:'内容',content:'内容',role:'发言方',type:'事件类型',kind:'记录类型',from:'原状态',to:'新状态',
  provider:'执行工具',model:'使用模型',elapsed:'执行耗时',exit_code:'程序退出结果',
  application_version:'应用自报版本',protocol:'监测协议',monitor_mode:'监测方式',release_monitor_available:'自动发布监测已接入',
  health:'运行健康情况',host:'服务状态',activity:'会话与任务活动',desktop:'桌面活动',
  known:'活动信息可读取',sessions:'活动会话数',jobs:'活动任务数',foreground:'窗口在前台',voiceBusy:'语音正在使用',
  problem_resolved:'原需求效果已验证',uncertain:'执行结果尚待核对',pause_verified:'暂停证据已核实',
  observation:'仅作观察',source_unchanged:'源码未发生变化',processes_stopped:'执行进程已停止',
  signals:'发现的信号',source:'信号来源',component:'影响模块',classification:'处理分类',in_scope:'属于项目范围',
  priority:'优先级',count:'累计出现次数',revisions:'返修次数',execution_seconds:'累计执行耗时',
  passed:'通过数量',blocked:'阻塞数量',target:'目标数量',target_count:'评测任务数',target_phase:'评测终点',
  method:'巡查方式',environment:'执行环境',results:'各项结果',steps:'巡查步骤',
  at:'记录时间',created:'创建时间',updated:'更新时间',started:'开始时间',finished:'结束时间',
  started_at:'开始时间',finished_at:'结束时间',sampledAt:'采样时间',last_seen:'最近发现',
  tokens:'Token 用量',input_tokens:'输入 Token',output_tokens:'输出 Token',cached_input_tokens:'缓存输入 Token',
  usage:'模型用量',inputTokens:'输入 Token',outputTokens:'输出 Token',totalTokens:'总 Token',
  cacheReadTokens:'缓存读取 Token',cacheWriteTokens:'缓存写入 Token',
  required:'是否必须通过',pause_proof:'暂停核对结果',scope:'审查范围',
  receipts:'执行回执',packet:'审查输入',suggestion:'审查建议',reviewer:'审查模型',
  requirement_id:'关联需求',run_id:'关联任务',delivery_id:'关联交付批次',release_id:'关联发布',
  signal_ids:'来源信号',inspection_id:'关联巡查',evidence_id:'关联证据',review_id:'关联审查',
  acceptance_review_id:'验收审查',review_packet:'审查输入',session_id:'关联会话',
};
const values: Record<string,string> = {
  pending_confirmation:'待确认',waiting_for_reply:'等待协作回复',rejected:'已拒绝',
  daily_attribution:'晚间统一归因',duplicate:'重复问题',no_issue:'无新问题',requirement:'形成需求',
  daily_acceptance:'晚间逐项复验',daily_retrospective:'日报与复盘',
  products:'项目',runs:'研发任务',requirements:'需求',daily_reports:'日报',
  autopilot_created:'建立研发记录',autopilot_transition:'研发阶段变更',autopilot_monitor_error:'巡检异常',
  basic:'基础健康检查（兼容旧版）',full:'完整认证监测','thsoctop-basic-status':'旧版基础状态接口','thsoctop-monitor-v1':'认证运行监测协议',
  investigate:'调查取证',investigating:'调查取证中',awaiting_external:'等待外部条件（自动复查）',resolved:'调查确认已解决',waiting:'等待外部条件',
  ...labels,pass:'通过',fail:'未通过',skipped:'未执行',busy:'暂不具备执行条件',observed:'已记录，尚待判断',
  agent_execution:'Agent 运行异常（不代表代码评审结论）',
  repair_feature:'修复 feat 评审问题',repair_release:'修复 release 验证问题',repair:'问题修复',review:'代码评审',
  delivered:'已交付未上线',online:'已上线',collecting:'等待 23:30 统一验证',validating:'release 统一验证',review_failed:'代码评审失败',merging_feature:'评审通过 · 合入 release',repairing_feature:'修复评审问题',syncing_feature:'同步评审 MR',syncing_release:'同步上线 MR',reviewing_release:'release 变更复审',repairing_release:'修复验证问题',release_failed:'统一验证失败',review_feature:'Code Review · feat → release',review_release:'release 变更复审',validate_release:'release 统一验证',merge_feature:'合入 release',merge_release:'合入 master',code_review:'代码评审',repairing:'问题修复',awaiting_merge:'待合并',syncing:'同步主分支',merging:'确认合并',preparing:'整合验收成果',
  accepted:'业务验收通过，待交付',pending:'待处理',queued:'排队等待',reviewed:'已评估',classified:'已生成需求',
  superseded:'已被后续记录替代',planning:'制定方案',plan_review:'方案审查',verifying:'验证中',
  acceptance_review:'最终验收',awaiting_release:'等待发布条件',deploying:'正在发布',observing:'上线观察中',
  rolled_back:'已回滚',rollback_pending:'等待回滚',pausing:'正在暂停',cancelling:'正在取消',
  'recover-runtime':'应用自动恢复',acceptance_infrastructure:'验收环境异常',
  master_sync:'master 实例更新',final_acceptance:'master 最终验收',probe:'运行监测',inspect:'体验巡检',discover:'需求发现',plan:'制定方案',develop:'开发实现',
  verify:'独立验证',idle:'空闲检查',publish:'发布上线',observe:'上线观察',rollback:'回滚恢复',
  codex:'Codex / GPT',harness:'DeepSeek Harness',claude:'Claude',ready:'已就绪',stopped:'已停止',
  development:'开发需求',investigation:'待调查',environment:'环境问题',isolated:'隔离测试环境',
  runtime:'运行监测',inspection:'体验巡检',feedback:'用户反馈',active:'自主运行',paused:'已暂停',
  user:'用户',assistant:'助手',system:'系统',tool:'工具',
  'session/header':'会话已建立','session/title':'更新会话标题','user/message':'用户发送消息',
  'assistant/message':'助手回复','autopilot/process-completed':'执行步骤已结束',
};
const timeFields = new Set(['next_auto_retry_at','last_auto_retry_at','next_investigation','repair_deferred_until','cutoff','at','created','updated','started','finished','started_at','finished_at','sampledAt','last_seen']);
const isRecord = (value: unknown): value is RecordData => !!value && typeof value === 'object' && !Array.isArray(value);
export function unpack(value:unknown):unknown {
  if(typeof value==='string' && /^[\[{]/.test(value.trim())) {
    try { const parsed:unknown=JSON.parse(value); if(parsed && typeof parsed==='object')return parsed; } catch { /* Plain text remains visible. */ }
  }
  return value;
}
export function recordLabel(value:unknown) {
  return typeof value==='string' ? eventLabels[value] || values[value] || value : '未记录';
}
export function recordSummary(value:unknown):string {
  const parsed=unpack(value);
  if(isRecord(parsed)) {
    if(parsed.error_info || ['fail','failed','blocked'].includes(String(parsed.status)))return errorText(parsed);
    const main=parsed.reason || parsed.summary || parsed.message || parsed.actual || parsed.title;
    return main ? recordSummary(main) : parsed.status ? recordLabel(parsed.status)+'，展开查看详情' : '结构化记录，展开查看详情';
  }
  if(Array.isArray(parsed))return `${parsed.length} 条记录，展开查看详情`;
  return parsed==null?'未记录':String(parsed);
}
export function RawRecord({value}:{value:unknown}) {
  return <TechnicalDetails value={value}/>;
}
export function RecordValue({value,field='',depth=0}:{value:unknown;field?:string;depth?:number}) {
  const parsed=unpack(value);
  if(parsed==null)return <span className="muted">未记录</span>;
  if(depth>7)return <RawRecord value={parsed}/>;
  if(field==='error')return <ErrorNotice value={parsed}/>;
  if(field==='diagnostic')return <TechnicalDetails value={parsed}/>;
  if(field==='checks')return <CheckList value={parsed} depth={depth+1}/>;
  if(field==='receipts')return <ReceiptList value={parsed}/>;
  if(Array.isArray(parsed))return parsed.length?<div className="record-items">{parsed.map((item,index)=><div key={index} className="record-item"><RecordValue value={item} depth={depth+1}/></div>)}</div>:<span className="muted">暂无记录</span>;
  if(isRecord(parsed)) {
    const entries=Object.entries(parsed).filter(([key,item])=>names[key] && item!==undefined && item!==null && !(parsed.error_info && ['reason','error','summary','actual','detail','error_info','diagnostic'].includes(key)));
    const extra=Object.keys(parsed).filter(key=>!names[key]);
    return <div className="record-value">{!!parsed.error_info && <ErrorNotice value={parsed}/>} {entries.length>0?<Descriptions size="small" column={1} items={entries.map(([key,item])=>({key,label:names[key],children:['summary','actual'].includes(key) && ['fail','failed','blocked'].includes(String(parsed.status))?<ErrorNotice value={item}/>:<RecordValue value={item} field={key} depth={depth+1}/>}))}/>:<span className="muted">这条记录只有技术字段，可展开原始数据查看。</span>}{extra.length>0 && !parsed.error_info && <RawRecord value={parsed}/>}</div>;
  }
  if(typeof parsed==='boolean')return <span>{field==='problem_resolved'?(parsed?'已验证':'尚未验证'):parsed?'是':'否'}</span>;
  if(timeFields.has(field) && (typeof parsed==='number' || typeof parsed==='string'))return <span>{time(parsed)}</span>;
  if(['exit_code','adapter_exit_code'].includes(field))return <TechnicalDetails value={{[field]:parsed}}/>;
  if(typeof parsed==='number')return <span>{['elapsed','execution_seconds'].includes(field)?`${Math.round(parsed)} 秒`:['exit_code','adapter_exit_code'].includes(field)?'退出码 '+parsed:parsed.toLocaleString()}</span>;
  if(['reason','error','reporting_error','feedback'].includes(field))return <ErrorNotice value={parsed}/>;
  if(['detail','details'].includes(field) && typeof parsed==='string')return <TechnicalDetails value={parsed}/>;
  return <span className="record-text">{['failure_kind','monitor_mode','protocol','outcome','status','decision','from','to','phase','action','provider','host','classification','environment','source','role','type','kind','target_phase'].includes(field)?recordLabel(parsed):String(parsed)}</span>;
}
function ResultTag({status}:{status:unknown}) {
  const color=['pass','completed','done','accepted'].includes(String(status))?'success':['fail','failed','blocked'].includes(String(status))?'error':['skipped','busy','unknown'].includes(String(status))?'warning':'default';
  return <Tag color={color}>{status ? recordLabel(status) : '未记录结果'}</Tag>;
}
function checkName(name:unknown) {
  if(typeof name!=='string')return '检查项目';
  if(name.startsWith('/') || name.startsWith('pnpm ') || name.startsWith('bash ')) {
    if(/typecheck/.test(name))return '类型检查';
    if(/pnpm (?:run )?build/.test(name))return '项目构建';
    if(/pnpm (?:run )?test/.test(name))return '自动化测试';
    if(/install.*frozen-lockfile/.test(name))return '依赖安装检查';
    if(/vendor:check/.test(name))return '依赖文件完整性检查';
    if(/dsh:compat/.test(name))return '运行时兼容性检查';
    if(/verify-/.test(name))return '功能集成检查';
    return '执行验证命令';
  }
  return name;
}
export function CheckList({value,depth=0}:{value:unknown;depth?:number}) {
  const parsed=unpack(value);
  if(!Array.isArray(parsed))return parsed==null?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无检查回执"/>:<RecordValue value={parsed} depth={depth+1}/>;
  if(!parsed.length)return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无检查回执"/>;
  const checks=parsed.filter(isRecord);
  const passed=checks.filter(c=>c.status==='pass').length;
  const failed=checks.filter(c=>['fail','failed','blocked'].includes(String(c.status))).length;
  return <div className="record-checks">{checks.length>0 && <p className="muted">共 {checks.length} 项检查 · {passed} 项通过 · {failed} 项未通过或阻塞 · {checks.length-passed-failed} 项未确认通过</p>}{parsed.map((item,index)=>{
    if(!isRecord(item))return <div className="record-item" key={index}><RecordValue value={item} depth={depth+1}/></div>;
    const summary=item.reason || item.summary || item.actual;
    return <Card size="small" key={index} className="record-check"><Space wrap><strong>{checkName(item.name || item.title)}</strong><ResultTag status={item.status}/><Tag>{item.required===false?'非必需项':item.required===true?'必需项':'未标注是否必需'}</Tag></Space>{summary!=null && <div className="record-note">{['fail','failed','blocked'].includes(String(item.status))?<ErrorNotice value={item}/>:<RecordValue value={summary} depth={depth+1}/>}</div>}<RawRecord value={item}/></Card>;
  })}</div>;
}
export function ResultDetails({value}:{value:unknown}) {
  const parsed=unpack(value);
  if(!isRecord(parsed))return <RecordValue value={parsed}/>;
  const {checks,...rest}=parsed;
  return <div className="record-result"><RecordValue value={rest}/>{checks!==undefined && <><h4>检查结果</h4><CheckList value={checks}/></>}</div>;
}
export function ReceiptList({value}:{value:unknown}) {
  const parsed=unpack(value);
  if(!Array.isArray(parsed) || !parsed.length)return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无执行步骤记录；过程截图可在“巡查与证据”查看"/>;
  return <div className="record-receipts">{[...parsed].reverse().map((receipt,index)=>{
    if(!isRecord(receipt))return <RecordValue key={index} value={receipt}/>;
    const unpacked=unpack(receipt.result);
    const result=isRecord(unpacked)?unpacked:{};
    return <Card size="small" key={String(receipt.call_id || index)}><Space wrap><strong>{recordLabel(receipt.action || receipt.phase)}</strong><ResultTag status={result.status || result.decision}/><span className="muted">{time(receipt.at as number)}</span></Space><div className="record-note">{['fail','failed','blocked','busy'].includes(String(result.status))?<ErrorNotice value={result}/>:<RecordValue value={result.reason || result.summary || result.instruction || '该步骤未记录文字说明，请查看详情。'}/>}</div><details className="record-more"><summary>查看步骤详情</summary><ResultDetails value={unpacked ?? receipt}/></details></Card>;
  })}</div>;
}
export function EventRecords({value}:{value:unknown}) {
  if(!Array.isArray(value) || !value.length)return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无会话事件"/>;
  return <div className="record-receipts">{[...value].reverse().map((item,index)=><Card key={index} size="small" title={isRecord(item)?recordLabel(item.type || item.kind || '会话事件'):'会话事件'}><RecordValue value={item}/></Card>)}</div>;
}
