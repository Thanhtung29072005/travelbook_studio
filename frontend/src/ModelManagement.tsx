import {useEffect,useState} from 'react'
import { Settings2, RefreshCw, ArrowUp, ArrowDown, Trash2, Plus, CheckCircle2, AlertCircle, Save, Sparkles } from 'lucide-react'
import {api} from './api'
import type {RouterCatalog} from './types'

export function RouterModels() {
  const [catalog,setCatalog]=useState<RouterCatalog>(),[chain,setChain]=useState<string[]>([]),[candidate,setCandidate]=useState('')
  const [image,setImage]=useState(''),[imageFallback,setImageFallback]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
  function populate(value:RouterCatalog){setCatalog(value);setChain(value.models);setImage(value.image_model);setImageFallback((value.image_fallback_models||[]).join(', '))}
  useEffect(()=>{let live=true;api.models().then(value=>{if(live)populate(value)}).catch(e=>{if(live)setError(String(e.message))});return()=>{live=false}},[])
  async function act(action:()=>Promise<void>){setBusy(true);setError('');setNotice('');try{await action()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  function move(index:number,direction:number){setChain(old=>{const next=[...old];[next[index],next[index+direction]]=[next[index+direction],next[index]];return next})}
  return <main className="content"><section className="workspace model-workspace">
    <div className="section-title">
      <div>
        <p className="eyebrow">ROUTER CONFIGURATION</p>
        <h1>Cấu hình Model 9Router</h1>
      </div>
      <button className="sync-models-btn" disabled={busy} onClick={()=>act(async()=>{const synced=await api.syncModels();setCatalog(synced);setNotice('Đã đồng bộ danh sách model từ 9Router. Các lựa chọn đang chỉnh được giữ nguyên.')})}>
        <RefreshCw size={14} className={busy?'spin':''}/> Đồng bộ 9Router
      </button>
    </div>
    <p>Model đầu tiên là model chính dùng để xử lý; các model tiếp theo là fallback tự động khi gặp sự cố hoặc timeout.</p>
    <div className="model-meta-bar">
      <span>API Key: <strong>{catalog?.api_key_configured?'Đã cấu hình':'Chưa cấu hình'}</strong></span>
      <span>Đồng bộ gần nhất: <strong>{catalog?.synced_at||'Chưa đồng bộ'}</strong></span>
      <span>Số model khả dụng: <strong>{catalog?.available_models?.length||0}</strong></span>
    </div>
    {error&&<p role="alert" className="form-error"><AlertCircle size={15}/> {error}</p>}
    {notice&&<p role="status" className="demo-notice" style={{padding:'12px 16px',margin:'12px 0'}}><CheckCircle2 size={15} color="#2d8050"/> {notice}</p>}
    
    <h3>Chuỗi model chính và fallback ({chain.length})</h3>
    <ul className="model-chain-list">
      {chain.map((model,index)=>(
        <li className="model-chain-item" key={model}>
          <div style={{display:'flex',alignItems:'center',gap:'10px',minWidth:0}}>
            <span className={`badge ${index===0?'primary-tag':'fallback-tag'}`}>{index===0?'Chính':`Fallback #${index}`}</span>
            <strong style={{overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{model}</strong>
          </div>
          <div className="model-chain-actions">
            <button aria-label={`Ưu tiên ${model}`} disabled={busy||index===0} onClick={()=>move(index,-1)} title="Đẩy lên">
              <ArrowUp size={13}/> Lên
            </button>
            <button aria-label={`Hạ ưu tiên ${model}`} disabled={busy||index===chain.length-1} onClick={()=>move(index,1)} title="Hạ xuống">
              <ArrowDown size={13}/> Xuống
            </button>
            <button aria-label={`Bỏ ${model}`} disabled={busy||chain.length===1} onClick={()=>setChain(chain.filter(v=>v!==model))} title="Bỏ khỏi chuỗi" style={{color:'#a33924'}}>
              <Trash2 size={13}/> Bỏ
            </button>
          </div>
        </li>
      ))}
    </ul>

    <div className="model-form-group">
      <label>Thêm model vào chuỗi</label>
      <div style={{display:'flex',gap:'9px'}}>
        <input style={{flex:1}} list="global-model-catalog" placeholder="Chọn hoặc nhập ID model (ví dụ: gpt-4o, gemini-1.5-pro...)" value={candidate} onChange={e=>setCandidate(e.target.value)}/>
        <button className="secondary-action" disabled={busy||!candidate.trim()||chain.includes(candidate.trim())||chain.length>=20} onClick={()=>{setChain([...chain,candidate.trim()]);setCandidate('')}}>
          <Plus size={14}/> Thêm vào chuỗi
        </button>
      </div>
      <datalist id="global-model-catalog">{(catalog?.available_models||[]).map(model=><option key={model} value={model}/>)}</datalist>
    </div>

    <div className="model-form-group">
      <label>Model ảnh chính</label>
      <input list="global-model-catalog" placeholder="Model sinh ảnh hoặc vision (để trống để dùng ảnh dự phòng)" value={image} onChange={e=>setImage(e.target.value)}/>
    </div>

    <div className="model-form-group">
      <label>Fallback ảnh (ngăn bằng dấu phẩy)</label>
      <input placeholder="ví dụ: gpt-image-1, sd-model-2" value={imageFallback} onChange={e=>setImageFallback(e.target.value)}/>
      <small style={{color:'#6f756e',fontSize:'11px',marginTop:'4px'}}>Có thể nhập ID thủ công khi 9Router chưa đồng bộ. Để trống model ảnh để dùng ảnh dự phòng.</small>
    </div>

    <div style={{marginTop:'26px'}}>
      <button className="primary" disabled={busy||!chain.length} onClick={()=>act(async()=>{populate(await api.updateModels(chain,image.trim(),imageFallback.split(',').map(s=>s.trim()).filter(Boolean)));setNotice('Đã lưu cấu hình model thành công.')})}>
        <Save size={15}/> Lưu cấu hình model
      </button>
    </div>
  </section></main>
}
