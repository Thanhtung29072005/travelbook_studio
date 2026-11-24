import {useEffect,useState} from 'react'
import { Plus, RefreshCw, Pause, Play, Edit3, Trash2, AlertTriangle, ShieldCheck } from 'lucide-react'
import {api} from './api'
import type {AgentId,ProjectAgent,ProjectDetail} from './types'

const ROLES:AgentId[]=['researcher','planner','budget','content_editor','image_curator','illustrator','map_agent','reviewer','layout_editor']
export function CrewEditor({project,onChanged,onPause}:{project:ProjectDetail;onChanged:()=>Promise<void>;onPause?:()=>void}) {
  const [form,setForm]=useState<ProjectAgent|null>(null),[removing,setRemoving]=useState<ProjectAgent|null>(null)
  const [catalog,setCatalog]=useState<string[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
  const locked=['running','pause_requested'].includes(project.status)
  useEffect(()=>{let live=true;api.models().then(v=>{if(live)setCatalog(v.available_models)}).catch(()=>{});return()=>{live=false}},[])
  async function act(action:()=>Promise<unknown>,message:string){setBusy(true);setError('');setNotice('');try{await action();await onChanged();setForm(null);setRemoving(null);setNotice(message)}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  async function handleHoldOn(){if(onPause){onPause();return}await act(()=>api.pause(project.id),'Đã gửi yêu cầu tạm dừng.')}
  function draft(){setForm({id:'',name:'',role_key:'researcher',instructions:'',color:'#426b91',enabled:false,is_core:false,model:null,fallback_models:[],image_model:null,temperature:null})}
  const codeRole=form&&['budget','map_agent','layout_editor'].includes(form.role_key)
  return <section className="workspace">
    <div className="section-title">
      <div>
        <p className="eyebrow">CREW CONFIGURATION</p>
        <h2>Agent hỗ trợ</h2>
      </div>
      <span>{project.agents.length} agent cấu hình</span>
    </div>
    <p>PM luôn bật và không thể xóa. Mỗi vai trò có tối đa một hồ sơ đang bật; bật hồ sơ mới sẽ thay hồ sơ cũ. Vai trò bắt buộc bị tắt hoặc xóa sẽ chặn bước đó cho đến khi bật lại hoặc thêm agent thay thế.</p>
    <div className="crew-actions">
      <button className="primary" disabled={busy||locked} onClick={draft}><Plus size={15}/> Thêm agent</button>
      <button className="sync-models-btn" disabled={busy} onClick={()=>act(async()=>{setCatalog((await api.syncModels()).available_models)},'Đã đồng bộ model từ 9Router.')}>
        <RefreshCw size={14} className={busy?'spin':''}/> Đồng bộ model 9Router
      </button>
    </div>
    {locked&&(
      <div role="status" className="agent-hold-notice">
        <div className="agent-hold-info">
          <AlertTriangle size={20} color="#b47814" />
          <div>
            <strong>Dự án đang trong tiến trình {project.status==='pause_requested'?'chờ dừng an toàn':'hoạt động'}</strong>
            <p>Cấu hình agent bị khóa để bảo toàn dữ liệu. Bấm nút Tạm dừng (Hold on) để dừng an toàn ở checkpoint trước khi cấu hình.</p>
          </div>
        </div>
        {project.status==='running'&&(
          <button className="pause-button" disabled={busy} onClick={handleHoldOn}>
            <Pause size={14} fill="currentColor"/> Tạm dừng dự án (Hold on)
          </button>
        )}
      </div>
    )}
    {error&&<p role="alert" className="form-error"><AlertTriangle size={15}/> {error}</p>}
    {notice&&<p role="status" className="demo-notice" style={{padding:'10px 14px',margin:'10px 0'}}>{notice}</p>}
    <div className="agent-grid">{project.agents.map(agent=><article className="agent-card" key={agent.id} style={{borderTop:`3px solid ${agent.color}`}}>
      <h3>{agent.name}</h3>
      <p>{agent.role_key} · {agent.enabled?'Đang bật':'Đã tắt'}{agent.role_key==='pm'?' · PM bảo vệ':''}</p>
      <small>Chính: {agent.model||'Theo cấu hình chung'} · Fallback: {agent.fallback_models.join(' → ')||'Theo cấu hình chung'}</small>
      <p>Ảnh / vision: {agent.image_model||'Theo cấu hình chung'}</p>
      <div className="crew-actions">
        <button className="secondary-action" disabled={busy||locked} onClick={()=>setForm({...agent,fallback_models:[...agent.fallback_models]})}>
          <Edit3 size={13}/> Chỉnh sửa
        </button>
        {agent.role_key!=='pm'&&<>
          <button className={`agent-hold-btn ${agent.enabled?'is-enabled':'is-disabled'}`} disabled={busy||locked} onClick={()=>act(()=>api.updateAgent(project.id,agent.id,{enabled:!agent.enabled}),agent.enabled?'Đã tạm ngưng agent.':'Đã kích hoạt agent.')}>
            {agent.enabled ? <><Pause size={13} fill="currentColor"/> Tạm ngưng (Hold on)</> : <><Play size={13}/> Kích hoạt</>}
          </button>
          <button className="danger-action" disabled={busy||locked} onClick={()=>setRemoving(agent)}>
            <Trash2 size={13}/> Xóa
          </button>
        </>}
      </div>
    </article>)}</div>
    {removing&&<div className="modal-backdrop"><section role="dialog" aria-label="Xóa agent" className="modal"><h2>Xóa {removing.name}?</h2><p>Cấu hình agent này sẽ bị xóa. Những sản phẩm và sự kiện đã tạo được giữ lại.</p><div className="modal-actions"><button className="secondary" disabled={busy} onClick={()=>setRemoving(null)}>Hủy</button><button className="danger-button" disabled={busy||locked} onClick={()=>act(()=>api.removeAgent(project.id,removing.id),'Đã xóa agent.')}>Xác nhận xóa</button></div></section></div>}
    {form&&<div className="modal-backdrop"><section role="dialog" aria-label="Cấu hình agent" className="modal crew-form"><h2>{form.id?'Chỉnh sửa agent':'Thêm agent hỗ trợ'}</h2>
      <label>Tên agent<input value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label>
      <label>Vai trò<select disabled={!!form.id} value={form.role_key} onChange={e=>setForm({...form,role_key:e.target.value as AgentId})}>{(form.role_key==='pm'?['pm']:ROLES).map(role=><option key={role}>{role}</option>)}</select></label>
      <label>Màu<input type="color" value={form.color} onChange={e=>setForm({...form,color:e.target.value})}/></label>
      <label>System instructions<textarea disabled={!!codeRole} value={form.instructions} onChange={e=>setForm({...form,instructions:e.target.value})}/></label>
      <datalist id="agent-model-catalog">{catalog.map(model=><option key={model} value={model}/>)}</datalist>
      <label>Model chính<input list="agent-model-catalog" disabled={!!codeRole||form.role_key==='illustrator'} value={form.model||''} onChange={e=>setForm({...form,model:e.target.value||null})}/></label>
      <label>Fallback theo thứ tự (dấu phẩy)<input disabled={!!codeRole} value={form.fallback_models.join(',')} onChange={e=>setForm({...form,fallback_models:e.target.value.split(',')})}/></label>
      <label>Model ảnh / vision<input list="agent-model-catalog" disabled={!['illustrator','reviewer'].includes(form.role_key)} value={form.image_model||''} onChange={e=>setForm({...form,image_model:e.target.value||null})}/></label>
      <label>Temperature<input type="number" min="0" max="2" step="0.1" disabled={!!codeRole||form.role_key==='illustrator'} value={form.temperature??''} onChange={e=>setForm({...form,temperature:e.target.value===''?null:Number(e.target.value)})}/></label>
      <p>Để trống để dùng model chung. Budget, Map và Layout chạy bằng code. Hồ sơ mới được tạo ở trạng thái tắt.</p><div className="modal-actions"><button className="secondary" disabled={busy} onClick={()=>setForm(null)}>Hủy</button><button className="primary" disabled={busy||locked||!form.name.trim()} onClick={()=>act(()=>{const {id,is_core,enabled,role_key,...fields}=form;const body={...fields,fallback_models:fields.fallback_models.map(s=>s.trim()).filter(Boolean)};return id?api.updateAgent(project.id,id,body):api.addAgent(project.id,{...body,role_key})},'Đã lưu agent.')}>Lưu agent</button></div>
    </section></div>}
  </section>
}
