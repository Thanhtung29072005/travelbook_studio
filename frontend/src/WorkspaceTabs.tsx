import { useState } from 'react'
import { api } from './api'
import type { AgentId, Approval, EventContent, EventItem, ProjectDetail, Task } from './types'

export const ROLES: AgentId[] = ['pm','researcher','planner','budget','content_editor','image_curator','illustrator','map_agent','reviewer','layout_editor']
const STAGES: Record<string,string> = {plan:'Kế hoạch',itinerary:'Lịch trình',visuals:'Hình ảnh & kiểm duyệt',final:'PDF cuối'}
const message = (error: unknown) => error instanceof Error ? error.message : String(error)

export function Schedule({tasks}: {tasks: Task[]}) {
  const dated = tasks.flatMap(task => {
    const start = Date.parse(task.start_at || ''), end = Date.parse(task.due_at || '')
    return Number.isFinite(start) && Number.isFinite(end) && end >= start ? [{task,start,end}] : []
  })
  const minimum = Math.min(...dated.map(item=>item.start)), maximum = Math.max(...dated.map(item=>item.end))
  const span = Math.max(maximum-minimum, 60000)
  return <section className="workspace"><h2>Lịch công việc</h2><p>Mốc thời gian do backend cung cấp; không tự tạo ngày cho task chưa có lịch.</p>
    {dated.map(({task,start,end})=><div className="schedule-row" key={task.id}><div><strong>{task.title}</strong><small>{task.agent} · {new Date(start).toLocaleString('vi-VN')} → {new Date(end).toLocaleString('vi-VN')}</small></div><div className="schedule-track"><i style={{left:`${(start-minimum)/span*100}%`,width:`${Math.max((end-start)/span*100,1)}%`}} title={task.status}/></div></div>)}
    <h3>Chưa có mốc thời gian</h3>{tasks.filter(task=>!dated.some(item=>item.task.id===task.id)).map(task=><div className="schedule-row" key={task.id}><strong>{task.title}</strong><span>{task.agent} · {task.status}</span></div>)}
    {!tasks.length && <p>Chờ PM tạo kế hoạch công việc.</p>}</section>
}

export { CrewEditor } from './CrewManagement'

export function ApprovalPanel({project,onChanged}:{project:ProjectDetail;onChanged:()=>Promise<void>}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  const locked=['running','pause_requested'].includes(project.status)
  async function act(action:()=>Promise<unknown>) {setBusy(true);setError('');try{await action();await onChanged()}catch(e){setError(message(e))}finally{setBusy(false)}}
  const stages=project.approval_stages||[]
  const enabled=project.approval_enabled===true
  function toggleStage(key:string,checked:boolean) {
    const selected=checked?[...stages,key]:stages.filter(stage=>stage!==key)
    return act(()=>api.approvalSettings(project.id,selected.length>0,selected))
  }
  return <section className="approval-panel"><h3>Người dùng duyệt các giai đoạn</h3><label><input type="checkbox" disabled={busy||locked} checked={enabled} onChange={e=>act(()=>api.approvalSettings(project.id,e.target.checked,stages.length?stages:Object.keys(STAGES)))}/> Bật duyệt trước khi đi tiếp</label>
    <p className="approval-mode" role="status">{enabled&&stages.length>0?'Đã bật. Dự án sẽ dừng tại các giai đoạn được chọn và chờ bạn duyệt để tiếp tục.':'Đang chạy tự động. Bật duyệt trước khi đi tiếp để dự án dừng chờ quyết định của bạn.'}</p>
    <fieldset className="approval-stage-options" disabled={busy||locked||!enabled} aria-label="Các giai đoạn cần duyệt"><div className="crew-actions">{Object.entries(STAGES).map(([key,label])=><label key={key}><input type="checkbox" checked={enabled&&stages.includes(key)} onChange={e=>toggleStage(key,e.target.checked)}/>{label}</label>)}</div></fieldset>
    {locked&&<p className="approval-hint">Tạm dừng dự án để thay đổi các giai đoạn cần duyệt.</p>}
    {project.status==='completed'&&<p className="approval-hint">Dự án đã hoàn tất. Chọn “Tôi muốn duyệt từng giai đoạn” khi tạo dự án mới để dừng duyệt trong quá trình chạy.</p>}
    {(project.approvals||[]).map(item=><ApprovalCard key={item.id} item={item} project={project} busy={busy} act={act}/>)}{error&&<p role="alert" className="form-error">{error}</p>}

  </section>
}

function ApprovalCard({item,project,busy,act}:{item:Approval;project:ProjectDetail;busy:boolean;act:(action:()=>Promise<unknown>)=>Promise<void>}) {
  const defaults: Record<string,AgentId[]> = {plan:['pm'],itinerary:['planner'],visuals:['image_curator','illustrator','map_agent'],final:['layout_editor']}
  const targets = item.allowed_targets || defaults[item.stage] || ['planner']
  const [feedback,setFeedback] = useState(''), [target,setTarget] = useState<AgentId>(targets[0])
  const disabled = busy || project.status !== 'awaiting_approval'
  const photos = (Array.isArray(item.content.photos)?item.content.photos:[]) as {id:string;place_id:string;url:{value:string};author:{value:string};license:{value:string};source_url:{value:string}}[]
  const assets = [item.content.final_pdf,...(Array.isArray(item.content.illustrations)?item.content.illustrations:[]),...(Array.isArray(item.content.maps)?item.content.maps:[])].filter(Boolean) as {id:string;name:string;mime_type:string}[]
  return <article className="approval-entry"><strong>{STAGES[item.stage]||item.stage}</strong><span> · {item.status==='pending'?'Chờ duyệt':item.status==='approved'?'Đã duyệt':'Đã yêu cầu sửa'}</span>
    <div className="event-assets">{photos.map(photo=><a key={photo.id} href={photo.source_url.value} target="_blank" rel="noreferrer"><img loading="lazy" referrerPolicy="no-referrer" src={photo.url.value} alt={`Ảnh ${photo.place_id}`}/><span>{photo.place_id} · {photo.author.value} · {photo.license.value}</span></a>)}</div>
    <div className="event-assets">{assets.map(asset=><a key={asset.id} href={api.downloadUrl(`/api/projects/${project.id}/artifacts/${asset.id}/download`)} target="_blank" rel="noreferrer">{asset.mime_type.startsWith('image/')&&<img alt={asset.name} src={api.downloadUrl(`/api/projects/${project.id}/artifacts/${asset.id}/download`)}/>} {asset.mime_type==='application/pdf'?'Mở PDF để duyệt':asset.name}</a>)}</div>
    <details open={item.status==='pending'&&item.stage==='itinerary'}><summary>Nội dung cần duyệt, nguồn và giấy phép ảnh</summary><HumanContent value={item.content}/></details>
    {item.status==='pending'?<><p>{item.stage==='visuals'?'Duyệt và đi tiếp sẽ chuyển sang dàn trang PDF. Chọn Yêu cầu sửa để giao agent xử lý phản hồi của bạn.':'Dự án đang chờ quyết định của bạn. Duyệt để đi tiếp hoặc gửi phản hồi cho agent sửa.'}</p><textarea aria-label="Phản hồi duyệt" placeholder="Nhập nội dung cần sửa" value={feedback} onChange={e=>setFeedback(e.target.value)}/><select aria-label="Agent sửa" value={target} onChange={e=>setTarget(e.target.value as AgentId)}>{targets.map(role=><option key={role}>{role}</option>)}</select><div className="crew-actions"><button className="primary" disabled={disabled} onClick={()=>act(()=>api.decide(project.id,item.id,'approve','',target))}>Duyệt và đi tiếp</button><button disabled={disabled||!feedback.trim()} onClick={()=>act(()=>api.decide(project.id,item.id,'revise',feedback,target))}>Yêu cầu sửa</button></div></>:item.feedback&&<p>Phản hồi: {item.feedback} · Agent: {item.target_agent}</p>}
  </article>
}

export function HumanContent({value,depth=0}:{value:unknown;depth?:number}) {
  if(value===null||value===undefined)return <span>—</span>
  if(depth>7)return <pre>{JSON.stringify(value,null,2)}</pre>
  if(Array.isArray(value))return <ol>{value.map((item,i)=><li key={i}><HumanContent value={item} depth={depth+1}/></li>)}</ol>
  if(typeof value==='object') {const item=value as Record<string,unknown>;if('value'in item&&'confidence'in item)return <div className="fact-value"><HumanContent value={item.value} depth={depth+1}/><small>Nguồn: {String(item.source)} · Độ tin cậy: {String(item.confidence)}</small></div>;return <dl>{Object.entries(item).map(([key,child])=><div key={key}><dt>{key.replaceAll('_',' ')}</dt><dd><HumanContent value={child} depth={depth+1}/></dd></div>)}</dl>}
  return <span>{String(value)}</span>
}

export function EventInspector({project,event,onChanged}:{project:ProjectDetail;event:EventItem;onChanged:()=>Promise<void>}) {
  const [content,setContent]=useState<EventContent>(),[open,setOpen]=useState(false),[editing,setEditing]=useState(false),[json,setJson]=useState(''),[feedback,setFeedback]=useState(''),[target,setTarget]=useState<AgentId>('planner'),[busy,setBusy]=useState(false),[error,setError]=useState('')
  async function load(){setError('');setBusy(true);try{const value=await api.eventContent(project.id,event.id);setContent(value);setJson(JSON.stringify(value.content,null,2));setOpen(true)}catch(e){setError(message(e))}finally{setBusy(false)}}
  async function act(action:()=>Promise<unknown>){setBusy(true);setError('');try{await action();await onChanged();await load();setEditing(false)}catch(e){setError(message(e))}finally{setBusy(false)}}
  return <div className="event-inspector"><button disabled={busy} onClick={()=>open?setOpen(false):load()}>{open?'Thu gọn':'Xem nội dung / phản hồi'}</button>{error&&<p role="alert">{error}</p>}{open&&content&&<div className="event-content"><p>{content.note}</p>{editing?<textarea className="json-editor" aria-label="JSON nội dung" value={json} onChange={e=>setJson(e.target.value)}/>:<HumanContent value={content.content}/>}
    <div className="crew-actions">{content.editable&&<button disabled={busy} onClick={()=>setEditing(!editing)}>{editing?'Hủy sửa':'Sửa JSON'}</button>}{editing&&<button disabled={busy} onClick={()=>act(()=>api.editContent(project.id,event.id,JSON.parse(json)))}>Lưu và kiểm tra schema</button>}</div>
    <div className="event-assets">{content.artifacts.map(item=><a key={item.id} href={api.downloadUrl(item.download_url)} target="_blank" rel="noreferrer">{item.mime_type.startsWith('image/')&&<img alt={item.name} src={api.downloadUrl(item.download_url)}/>} {item.name}</a>)}</div>
    {content.can_intervene&&<><textarea aria-label="Phản hồi agent" placeholder="Yêu cầu agent sửa hoặc làm bổ sung" value={feedback} onChange={e=>setFeedback(e.target.value)}/><select value={target} onChange={e=>setTarget(e.target.value as AgentId)}>{ROLES.map(role=><option key={role}>{role}</option>)}</select><button disabled={busy||!feedback.trim()} onClick={()=>act(()=>api.intervene(project.id,event.id,target,feedback))}>Giao lại cho agent</button></>}
  </div>}</div>
}

