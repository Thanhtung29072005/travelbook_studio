// Serve only a disposable UI fixture. No Python process, real keys or external API.
import {createServer} from 'vite'
import react from '@vitejs/plugin-react'
import {writeFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
const role=(name)=>({id:name,name,role_key:name,instructions:'',color:'#426b91',model:null,fallback_models:[],image_model:null,temperature:null,enabled:true,is_core:true})
const project={id:'ui-test',status:'paused',agents:['pm','researcher','reviewer','illustrator'].map(role)}
let catalog={router:'mock://9router',api_key_configured:true,models:['text-main','text-backup'],image_model:'image-main',image_fallback_models:[],available_models:['text-main','text-backup','image-main','image-backup','text-new'],synced_at:null}
const requests=[]
const server=await createServer({configFile:false,root:fileURLToPath(new URL('.',import.meta.url)),plugins:[{name:'management-fixture',configureServer(vite){vite.middlewares.use(async(req,res,next)=>{
  if(!req.url.startsWith('/api/'))return next()
  let raw='';for await(const chunk of req)raw+=chunk
  const body=raw?JSON.parse(raw):{};let value={};let status=200
  requests.push({method:req.method,path:req.url,body})
  if(req.url==='/api/projects/ui-test')value=project
  else if(req.url==='/api/router/models/sync'){catalog.synced_at=new Date().toISOString();value=catalog}
  else if(req.url==='/api/router/models'){if(req.method==='PUT')catalog={...catalog,...body};value=catalog}
  else if(req.url==='/api/projects/ui-test/agents'&&req.method==='POST'){
    if(body.role_key==='pm'){status=422;value={detail:'PM is protected'}}else{value={...role(body.role_key),...body,id:'ui-agent-'+Date.now(),is_core:false,enabled:false};project.agents.push(value);status=201}
  } else if(req.url.startsWith('/api/projects/ui-test/agents/')){
    const id=req.url.split('/').at(-1);const agent=project.agents.find(a=>a.id===id)
    if(!agent){status=404;value={detail:'Agent not found'}}
    else if(agent.role_key==='pm'&&(req.method==='DELETE'||body.enabled===false)){status=422;value={detail:'PM is protected'}}
    else if(req.method==='DELETE'){project.agents=project.agents.filter(a=>a.id!==id);value={status:'removed'}}
    else {if(body.enabled)for(const a of project.agents)if(a.role_key===agent.role_key)a.enabled=false;Object.assign(agent,body);value=agent}
  }else{status=404;value={detail:'Not found'}}
  await writeFile(new URL('../reference-review/agent-model-ui-requests.json',import.meta.url),JSON.stringify({requests,project,catalog},null,2))
  res.statusCode=status;res.setHeader('content-type','application/json');res.end(JSON.stringify(value))
})}},react()],server:{host:'127.0.0.1',port:5177,strictPort:true}})
await server.listen();console.log('Disposable management UI: http://127.0.0.1:5177/management-test.html')
