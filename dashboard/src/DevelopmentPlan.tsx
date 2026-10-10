import { useState } from 'react';
import { Segmented } from 'antd';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { RecordValue } from './RecordDetails';

export default function DevelopmentPlan({value}:{value:unknown}) {
  const [mode,setMode]=useState<string | number>('preview');
  const source=typeof value==='string'?value:JSON.stringify(value,null,2);
  return <section className="development-plan">
    <Segmented aria-label="方案显示方式" value={mode} onChange={setMode} options={[
      {label:'Markdown 预览',value:'preview'},
      {label:'源码',value:'source'},
    ]}/>
    {mode==='source'?<pre className="plan-source"><code>{source}</code></pre>:
      typeof value==='string'?<article className="plan-markdown"><Markdown remarkPlugins={[remarkGfm]} components={{
        a:({node:_,...props})=><a {...props} target="_blank" rel="noopener noreferrer"/>,
        table:({node:_,...props})=><div className="plan-table"><table {...props}/></div>,
      }}>{value}</Markdown></article>:<RecordValue value={value}/>}
  </section>;
}
