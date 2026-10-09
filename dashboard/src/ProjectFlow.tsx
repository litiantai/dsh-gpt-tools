import { useRef, useState } from 'react';
import { AimOutlined, ExpandOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons';

export type FlowNodeId = 'product'|'signals'|'requirements'|'review'|'planning'|'design'|'development'|'testing'|'acceptance'|'delivery'|'codeReview'|'fixing'|'merge'|'online'|'blocked'|'cancelled';
interface Node { id: FlowNodeId; title: string; label: string; detail: string; x: number; y: number; w: number; h: number; tone?: string }
const nodes: Node[] = [
  {id:'product',title:'项目总览',label:'当前项目',detail:'汇总需求，跟踪交付',x:40,y:140,w:200,h:150,tone:'blue'},
  {id:'signals',title:'巡检信号',label:'WATCH · 发现',detail:'运行异常与体验反馈',x:40,y:365,w:200,h:100},
  {id:'requirements',title:'需求池',label:'需求管理',detail:'证据已登记，等待评估',x:300,y:351,w:195,h:132},
  {id:'planning',title:'任务规划',label:'PLAN · 编排',detail:'隔离工作区，制定方案',x:548,y:369,w:176,h:98},
  {id:'review',title:'方案评审',label:'REVIEW · 审查',detail:'明确目标与验收标准',x:779,y:363,w:168,h:110},
  {id:'design',title:'方案与体验',label:'设计依据',detail:'已有方案，尚未进入开发',x:1014,y:150,w:174,h:102},
  {id:'development',title:'开发执行',label:'BUILD · 实现',detail:'独立执行器，按方案开发',x:1014,y:565,w:174,h:106},
  {id:'testing',title:'质量测试',label:'TEST · 验证',detail:'构建、回归与主链路检查',x:1248,y:565,w:174,h:106},
  {id:'acceptance',title:'业务验收',label:'REVIEW · 审查',detail:'核对证据，确认预期结果',x:1482,y:565,w:174,h:106},
  {id:'codeReview',title:'代码评审',label:'CODE REVIEW · MR',detail:'需求 PR · 数字为未评审需求数',x:1716,y:565,w:210,h:106},
  {id:'delivery',title:'已交付未上线',label:'RELEASE · 每日汇集',detail:'只包含代码评审通过的成果',x:1716,y:300,w:210,h:106},
  {id:'fixing',title:'问题修复',label:'FIX · 自动返修',detail:'未完成问题 · 同一 PR 可有多条',x:1482,y:220,w:174,h:106},
  {id:'merge',title:'待合并',label:'MERGE · 收口',detail:'release → master · 上线 PR',x:1986,y:180,w:174,h:106},
  {id:'online',title:'已上线',label:'MASTER · 已合入',detail:'已上线需求 · 按需求计数',x:1986,y:533,w:205,h:164,tone:'milestone'},
  {id:'blocked',title:'异常阻塞',label:'待处理',detail:'核对原因，处理后继续',x:1025,y:860,w:188,h:90,tone:'warning'},
  {id:'cancelled',title:'已终止',label:'保留记录',detail:'取消的任务和交付记录',x:548,y:670,w:176,h:95},
];
const paths = [
  {d:'M140 290 V365'}, {d:'M240 415 H300'}, {d:'M495 415 H548'}, {d:'M724 415 H779'},
  {d:'M947 396 H977 Q990 396 990 383 V213 Q990 201 1002 201 H1014',kind:'secondary'},
  {d:'M1101 252 V565',kind:'secondary'},
  {d:'M947 438 H977 Q990 438 990 451 V606 Q990 618 1002 618 H1014'},
  {d:'M1188 618 H1248'}, {d:'M1422 618 H1482'}, {d:'M1656 618 H1716'},
  // 正常交付、返修和复审使用独立入口，避免往返线路重叠。
  {d:'M1854 565 V406'},
  {d:'M1926 353 H1944 Q1956 353 1956 341 V245 Q1956 233 1968 233 H1986'},
  {d:'M2073 286 V533'},
  {d:'M1750 565 V517 Q1750 505 1738 505 H1702 Q1690 505 1690 493 V285 Q1690 273 1678 273 H1656',kind:'warning'},
  {d:'M1569 326 V438 Q1569 450 1581 450 H1789 Q1801 450 1801 462 V565',kind:'secondary',label:'修复后复审',x:1618,y:450},
  {d:'M863 363 V287 Q863 275 851 275 H452 Q440 275 440 287 V351',kind:'secondary',label:'评审意见',x:620,y:275},
  {d:'M842 473 V893 Q842 905 854 905 H1025',kind:'warning'},
  {d:'M1119 860 V787 Q1119 775 1107 775 H934 Q922 775 922 763 V473',kind:'secondary',label:'处理后继续',x:1020,y:775},
  {d:'M1335 671 V1008 Q1335 1020 1323 1020 H894 Q882 1020 882 1008 V473',label:'返修循环',x:1110,y:1020},
  {d:'M1569 671 V1128 Q1569 1140 1557 1140 H814 Q802 1140 802 1128 V473',kind:'secondary',label:'驳回修改',x:1360,y:1140},
  {d:'M636 467 V670',kind:'secondary'},
  {d:'M2191 615 H2204 Q2216 615 2216 603 V80 Q2216 60 2196 60 H368 Q348 60 348 80 V351',kind:'secondary',label:'主分支持续改进 · 新问题回流',x:1660,y:108},
];
const canvasWidth = 2246;
export default function ProjectFlow({counts,selected,onSelect,projectName}:{counts:Partial<Record<FlowNodeId,number>>;selected?:FlowNodeId;onSelect:(id:FlowNodeId)=>void;projectName:string}) {
  const container=useRef<HTMLDivElement>(null);
  const [zoom,setZoom]=useState(1);
  const [pan,setPan]=useState({x:0,y:0});
  const drag=useRef<{x:number;y:number;px:number;py:number}|null>(null);
  return <div className="project-flow" ref={container}>
    <div className="flow-section-labels"><span>01 / 持续发现</span><span>02 / 研发交付</span><span>03 / 运行反馈</span></div>
    <svg className="flow-canvas" style={{aspectRatio:`${canvasWidth} / 1220`}} viewBox={`${pan.x} ${pan.y} ${canvasWidth/zoom} ${1220/zoom}`} role="group" aria-label={`${projectName} 研发链路`} onPointerDown={e=>{
      if((e.target as Element).closest('button'))return;
      drag.current={x:e.clientX,y:e.clientY,px:pan.x,py:pan.y};e.currentTarget.setPointerCapture(e.pointerId);
    }} onPointerMove={e=>{if(drag.current){const scale=canvasWidth/zoom/e.currentTarget.clientWidth;setPan({x:drag.current.px-(e.clientX-drag.current.x)*scale,y:drag.current.py-(e.clientY-drag.current.y)*scale});}}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>
      <rect x="275" y="225" width="471" height="315" rx="25" className="flow-region"/>
      <text x="300" y="246" className="flow-region-label">需求发现与评审</text>
      {paths.map((edge,i)=><g key={i}>
        <path className="flow-edge-clearance" d={edge.d} aria-hidden="true"/>
        <path className={`flow-edge ${edge.kind || ''}`} d={edge.d}/>
        <g className={`flow-travel-arrow ${edge.kind || ''}`} aria-hidden="true" style={{offsetPath:`path('${edge.d}')`,animationDelay:`${-i*.37}s`,animationDuration:edge.d.length>35?'5s':'2.4s'}}>
          <path d="M-7 -7 L6 0 L-7 7 Z" fill="currentColor" stroke="none"/>
        </g>
        {edge.label && <g transform={`translate(${edge.x},${edge.y})`}><rect x={-edge.label.length*7-13} y="-34" width={edge.label.length*14+26} height="28" rx="14" className="flow-edge-label-bg"/><text textAnchor="middle" dy="-15" className="flow-edge-label">{edge.label}</text></g>}
      </g>)}
      {nodes.map(node=><foreignObject key={node.id} x={node.x} y={node.y} width={node.w} height={node.h}>
        <button type="button" className={`pipeline-node ${node.tone || ''} ${selected===node.id?'selected':''} ${(counts[node.id] || 0)>0?'has-items':''}`} onClick={()=>onSelect(node.id)} aria-label={`${node.title} ${counts[node.id] ?? 0}`} aria-pressed={selected===node.id}>
          <span className="pipeline-node-label">{node.id==='product'?projectName:node.label}</span>
          <span className="pipeline-node-title">{node.title}<b>{counts[node.id] ?? 0}</b></span>
          <span className="pipeline-node-detail">{node.detail}</span>
        </button>
      </foreignObject>)}
    </svg>
    <div className="flow-toolbar" aria-label="画布控制">
      <button title="适应画布" onClick={()=>{setZoom(1);setPan({x:0,y:0});}}><AimOutlined/></button>
      <button title="缩小" disabled={zoom<=.65} onClick={()=>setZoom(v=>Math.max(.65,v-.15))}><MinusOutlined/></button>
      <span>{Math.round(zoom*100)}%</span>
      <button title="放大" disabled={zoom>=2} onClick={()=>setZoom(v=>Math.min(2,v+.15))}><PlusOutlined/></button>
      <button title="全屏" onClick={()=>{if(document.fullscreenElement)void document.exitFullscreen();else void container.current?.requestFullscreen();}}><ExpandOutlined/></button>
    </div>
    <div className="flow-legend"><span><i/>研发流转</span><span><i className="dashed"/>反馈 / 返修</span><span><i className="amber"/>异常阻塞</span></div>
  </div>;
}
