const {test}=require('node:test')
const assert=require('node:assert/strict')
const {parseRoute,taskState,collectPages}=require('./src/ui.ts')
test('route keeps project and tab and rejects malformed IDs',()=>{assert.deepEqual(parseRoute('#projects/project-123/agents'),{page:'home',id:'project-123',tab:'agents'});assert.equal(parseRoute('#models').page,'models');assert.equal(parseRoute('#projects/../agents').id,undefined);assert.equal(parseRoute('#projects/a/invalid').tab,'overview')})
test('all six project tabs can be restored',()=>{for(const tab of ['overview','list','schedule','messages','agents','artifacts'])assert.equal(parseRoute('#projects/a/'+tab).tab,tab)})
test('explicit blocked/testing/approval status is preserved despite complete dependencies',()=>{for(const status of ['blocked','testing','awaiting_approval','done'])assert.equal(taskState({status,depends_on:[]},[]),status);assert.equal(taskState({status:'todo',depends_on:['missing']},[]),'blocked')})
test('pagination crosses initial cap and deduplicates identities',async()=>{const items=Array.from({length:237},(_,i)=>({id:String(i)}));const offsets=[];const result=await collectPages(async offset=>{offsets.push(offset);return {items:items.slice(offset,offset+100),total:items.length}});assert.equal(result.length,237);assert.deepEqual(offsets,[0,100,200])})
test('empty page terminates even if stale total claims more',async()=>{let calls=0;const result=await collectPages(async()=>{calls++;return {items:[],total:50}});assert.equal(calls,1);assert.equal(result.length,0)})

const {taskDetails,childTasks}=require('./src/taskDetails.ts')
test('existing projects get role-specific child descriptions',()=>{
  const descriptions=taskDetails({id:'old-project:researcher',agent:'researcher'})
  assert.equal(descriptions.length,3)
  assert.ok(descriptions.some(text=>text.includes('thời tiết')))
  assert.deepEqual(taskDetails({id:'custom',agent:'unknown'}),[])
})
test('PM finalization shows delivery steps separately from initial planning',()=>{
  const initial=taskDetails({id:'project:pm',agent:'pm'})
  const final=taskDetails({id:'project:pm_finalize',agent:'pm'})
  assert.notDeepEqual(initial,final)
  assert.ok(final.some(text=>text.includes('PDF')))
})
test('actual child tasks stay under their own parent in execution order',()=>{
  const children=[{id:'second',parent_id:'parent',order_index:2,status:'in_progress'},{id:'other',parent_id:'another',order_index:0},{id:'first',parent_id:'parent',order_index:1,status:'done'}]
  assert.deepEqual(childTasks({id:'parent'},children).map(task=>task.id),['first','second'])
  assert.equal(childTasks({id:'parent'},children)[1].status,'in_progress')
  assert.equal(children[0].id,'second')
})

const {workflowConnectionStatus}=require('./src/ui.ts')
test('workflow starts without arrows to pending or merely ready agents',()=>{
  for(const target of ['todo','ready']) assert.equal(workflowConnectionStatus('done',target,'running'),null)
  assert.equal(workflowConnectionStatus('todo','todo','queued'),null)
  assert.equal(workflowConnectionStatus('in_progress','todo','running'),null)
})
test('handoff arrows animate while receiving agent works and settle on completion',()=>{
  assert.equal(workflowConnectionStatus('done','in_progress','running'),'active')
  assert.equal(workflowConnectionStatus('done','testing','running'),'active')
  assert.equal(workflowConnectionStatus('done','done','running'),'done')
  assert.equal(workflowConnectionStatus('done','done','completed'),'done')
})
test('paused, approval and failure arrows remain visible without animation',()=>{
  assert.equal(workflowConnectionStatus('done','paused','paused'),'idle')
  assert.equal(workflowConnectionStatus('done','in_progress','paused'),'idle')
  assert.equal(workflowConnectionStatus('done','awaiting_approval','awaiting_approval'),'idle')
  assert.equal(workflowConnectionStatus('done','blocked','failed'),'idle')
})

const {artifactLibrary}=require('./src/artifactLibrary.ts')
function outputFile(id,kind,mime,name,created='2026-11-23T10:00:00Z'){
  return {id,kind,mime_type:mime,name,agent_id:kind,path:'/project/'+id,created_at:created,download_url:'/file/'+id}
}
test('published PDF stays featured even when it is between internal files and newer drafts',()=>{
  const pdf=outputFile('published','pdf','application/pdf','travelbook.pdf')
  const draft=outputFile('draft','pdf','application/pdf','travelbook.pdf','2026-11-23T11:00:00Z')
  const data=outputFile('data','research','application/json','research.json')
  const result=artifactLibrary([data,pdf,draft],{state:{final_pdf:{id:'published'}},final_pdf:pdf.path})
  assert.equal(result.pdf.id,'published')
  assert.deepEqual(result.internal.map(item=>item.id),['draft','data'])
})
test('gallery selects current images and collapses old versions and JSON into internal files',()=>{
  const old=outputFile('old','map','image/png','day1.png')
  const current=outputFile('current','map','image/png','day1.png','2026-11-23T11:00:00Z')
  const data=outputFile('json','illustration','application/json','illustrations.json')
  const result=artifactLibrary([old,current,data],{state:{maps:[{id:'current',path:current.path}]},final_pdf:null})
  assert.deepEqual(result.images.map(item=>item.id),['current'])
  assert.equal(result.internal.length,2)
})
test('legacy projects use latest PDF and image versions with stable ordering',()=>{
  const old=outputFile('old','illustration','image/png','cover.png')
  const current=outputFile('current','illustration','image/png','cover.png')
  const pdf=outputFile('pdf','pdf','application/pdf','travelbook.pdf')
  const decorations=['icon_location','icon_food','icon_transport','icon_culture','background_waves','background_dots']
    .map(name=>outputFile(name,'illustration','image/png',name+'.png'))
  const result=artifactLibrary([old,pdf,current,...decorations],{state:{},final_pdf:null})
  assert.equal(result.pdf.id,'pdf')
  assert.deepEqual(result.images.map(item=>item.id),['current'])
  assert.deepEqual(result.internal.map(item=>item.id),['old'])
  assert.equal(artifactLibrary([],{state:{},final_pdf:null}).pdf,undefined)
})
