import catalog from './error-catalog.json';

export type ErrorInfo = {code:string;message:string;suggestion:string};
const technical = /Traceback|\b(?:\w*Error|Exception)\b|Errno|https?:\/\/|\/(?:Users|home|tmp|var|private)\/|\b(?:QUOTA|ECONN\w*|ENOENT)\b|\b(?:pnpm|npm|node|python)\s|\{["']/i;
export function sanitizeDiagnostic(value:unknown):unknown {
  if(Array.isArray(value))return value.map(sanitizeDiagnostic);
  if(value && typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,/token|password|cookie|authorization|api.?key|secret|credential/i.test(k)?'[已隐藏]':sanitizeDiagnostic(v)]));
  if(typeof value!=='string')return value;
  return value.replace(/(Bearer\s+)[\w.\-/+=]+/gi,'$1[已隐藏]')
    .replace(/((?:["']?(?:api[_-]?key|token|password|cookie|authorization|secret|credential)["']?)\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)/gi,'$1[已隐藏]')
    .replace(/(https?:\/\/[^\s?]+)\?[^\s]*/g,'$1?[已隐藏]')
    .replace(/(https?:\/\/)[^/@\s]+:[^/@\s]+@/g,'$1[已隐藏]@').slice(0,20000);
}
export function describeError(value:unknown,subject='相关服务'):ErrorInfo {
  if(value && typeof value==='object') {
    const r=value as Record<string,unknown>;
    const info=r.error_info as ErrorInfo | undefined;
    if(info?.code && info.message && !technical.test(info.message))return {...info,suggestion:info.suggestion || ''};
    if(value instanceof Error)return describeError(value.message,subject);
    return describeError(r.reason ?? r.error ?? r.summary ?? r.message ?? '',subject);
  }
  const raw=String(value ?? '');
  if(raw.startsWith('{')) { try {return describeError(JSON.parse(raw),subject);} catch { /* Legacy plain text. */ } }
  const row=catalog.find(r=>new RegExp(r.pattern,'i').test(raw));
  if(!row && /[\u4e00-\u9fff]/.test(raw) && !technical.test(raw))return {code:'BUSINESS',message:raw,suggestion:''};
  const selected=row ?? catalog[catalog.length-1];
  return {code:selected.code,message:selected.message.replace('{subject}',subject),suggestion:selected.suggestion};
}
export function errorText(value:unknown,subject='相关服务') {
  const info=describeError(value,subject);return info.message+info.suggestion;
}
export function TechnicalDetails({value}:{value:unknown}) {
  let raw=value instanceof Error?{...value,message:value.message}:value;
  if(typeof raw==='string' && raw.startsWith('{')) {try {raw=JSON.parse(raw);} catch { /* Plain diagnostic. */ }}
  return <details className="record-raw"><summary>技术详情</summary><pre className="log">{JSON.stringify(sanitizeDiagnostic(raw),null,2)}</pre></details>;
}
export function ErrorNotice({value,subject='相关服务'}:{value:unknown;subject?:string}) {
  if(value==='—')return <span className="muted">—</span>;
  const info=describeError(value,subject);
  const plain=info.code==='BUSINESS' && typeof value==='string' && !value.startsWith('{');
  return <><span className="record-text">{info.message+info.suggestion}</span>{!plain && <TechnicalDetails value={value}/>}</>;
}
export class ApiError extends Error {
  error_info:ErrorInfo;
  diagnostic:unknown;
  constructor(value:unknown,subject='管理服务') {
    const info=describeError(value,subject);super(info.message+info.suggestion);
    this.error_info=info;this.diagnostic=sanitizeDiagnostic(value);
  }
}

export function serializeError(value:unknown):string {
  return JSON.stringify(sanitizeDiagnostic(value instanceof Error?{...value,message:value.message}:value));
}
