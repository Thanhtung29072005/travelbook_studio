import {useId, useState} from 'react'
import {ArrowRight, Check, MessageSquareText, RefreshCw} from 'lucide-react'
import {api} from './api'
import type {ProjectDetail} from './types'

export function ClarificationPanel({project,onChanged}:{project:ProjectDetail;onChanged:()=>Promise<void>}) {
  const [answer,setAnswer]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const answerId=useId()
  if(project.status!=='awaiting_input')return null
  const questions=Array.isArray(project.state.clarification_questions)?project.state.clarification_questions as {value:string}[]:[]
  const askingTimes=questions.length>0&&questions.every(question=>/giờ/i.test(question.value))
  async function submit(){
    if(busy||!answer.trim())return
    setBusy(true);setError('')
    try{await api.answer(project.id,answer.trim());setAnswer('');await onChanged()}
    catch(e){setError(e instanceof Error?e.message:String(e))}
    finally{setBusy(false)}
  }
  return <section className="clarification-panel" aria-labelledby={`${answerId}-title`}>
    <header className="clarification-header"><div className="clarification-heading"><span className="clarification-icon"><MessageSquareText size={23}/></span><div><h2 id={`${answerId}-title`}>Bổ sung thông tin chuyến đi</h2><p>Trả lời các câu hỏi bên dưới để PM hoàn thiện yêu cầu và tiếp tục lập cẩm nang.</p></div></div><span className="clarification-waiting"><i/>Chờ bạn trả lời</span></header>
    <div className="clarification-body">
      <div className="clarification-context"><h3>Thông tin cần làm rõ</h3>
        {questions.length>0?<ul className="clarification-questions">{questions.map((question,index)=><li key={index}><MessageSquareText size={15}/><span>{question.value}</span></li>)}</ul>:<p className="clarification-empty">Hãy bổ sung điểm đến, số ngày, số người và ngân sách của chuyến đi.</p>}
        <details className="clarification-brief"><summary>Yêu cầu bạn đã gửi</summary><p>{project.prompt}</p></details>
      </div>
      <form className="clarification-form" onSubmit={e=>{e.preventDefault();void submit()}} aria-busy={busy}>
        <label htmlFor={answerId}>Câu trả lời của bạn</label><p id={`${answerId}-hint`}>{askingTimes?'Giờ đến ngày đầu và giờ rời điểm du lịch ngày cuối. Không cần khai giờ về đến nhà.':'Bạn có thể trả lời tất cả câu hỏi trong cùng một nội dung.'}</p>
        <textarea id={answerId} aria-describedby={`${answerId}-hint ${answerId}-count`} maxLength={4000} rows={6} disabled={busy} value={answer} placeholder={askingTimes?'Ví dụ: Đến 10h, về 18h.':'Ví dụ: Ngân sách 10 triệu VND cho cả chuyến đi, gồm 2 người lớn. Tôi muốn ưu tiên tham quan và ẩm thực địa phương.'} onChange={e=>setAnswer(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();void submit()}}}/>
        <div className="clarification-input-meta"><span>Ctrl / ⌘ + Enter để gửi</span><span id={`${answerId}-count`}>{answer.length.toLocaleString('vi-VN')} / 4.000 ký tự</span></div>
        {error&&<p className="form-error" role="alert">{error}</p>}
        <div className="clarification-submit"><span><Check size={14}/>Tiếp tục sau khi gửi câu trả lời</span><button type="submit" className="primary" disabled={busy||!answer.trim()}>{busy?<RefreshCw size={16} className="spin"/>:<ArrowRight size={16}/>} {busy?'Đang gửi…':'Gửi và tiếp tục'}</button></div>
      </form>
    </div>
  </section>
}
