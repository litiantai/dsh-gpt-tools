/** Runs the real connector against a deterministic in-process Harness API double. */
import { readFile, appendFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { apply } from '../connector/dist/index.js'
const meta=JSON.parse(await readFile(process.argv[2],'utf8'))
const events=[]
let dispose=()=>{}
const api={sessions:{
  prompt:async request=>{
    const event={type:'user/message',data:{message:{source:{rpcId:request.rpcId},content:request.payload.content}}}
    events.push({event})
    await appendFile(join(meta.state,'sent-messages.jsonl'),JSON.stringify(request)+'\n')
    return {rpcId:request.rpcId,result:{ok:true,value:{accepted:true}}}
  },
  cancel:async request=>{
    await writeFile(join(meta.state,'stopped.json'),JSON.stringify(request))
    return {rpcId:request.rpcId,result:{ok:true,value:{accepted:true}}}
  },
  history:async request=>({rpcId:request.rpcId,result:{ok:true,value:{events,hasMore:false}}})
}}
apply({apiProxy:api,effect:fn=>{dispose=fn()}},{home:meta.home,stateDir:meta.state,origin:meta.origin})
process.on('SIGTERM',()=>{dispose();process.exit(0)})
