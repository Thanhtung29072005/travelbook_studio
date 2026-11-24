import { RouterModels } from './ModelManagement'
import { ClarificationPanel } from './ClarificationPanel'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import {
  Activity, CalendarDays, Users, AlertTriangle, ArrowRight, BookOpen, Check, ChevronRight,
  Circle, GalleryVerticalEnd, ListTodo,
  LayoutDashboard, MapPin, MessageSquareText, Pause, Play, Plus, RefreshCw,
  Search, Settings2, Sparkles, Square, Trash2, X,
} from 'lucide-react'
import { api } from './api'
import { Artifacts } from './Artifacts'
import { childTasks, taskDetails } from './taskDetails'
import type { AgentId, Artifact, EventItem, Project, ProjectDetail, Task, ProjectAgent } from './types'

/* ── Agent metadata ── */
const AGENTS: Record<string, { name: string; role: string; color: string; mark: string }> = {
  pm: { name: 'PM', role: 'Quản lý dự án', color: '#263f35', mark: 'PM' },
  researcher: { name: 'Researcher', role: 'Nghiên cứu', color: '#426b91', mark: 'NC' },
  planner: { name: 'Planner', role: 'Lập kế hoạch', color: '#2d8050', mark: 'KH' },
  budget: { name: 'Budget', role: 'Ngân sách', color: '#d99b2b', mark: 'NS' },
  content_editor: { name: 'Content', role: 'Biên tập nội dung', color: '#dd5c3d', mark: 'ND' },
  image_curator: { name: 'Image Curator', role: 'Ảnh minh họa', color: '#7d5c8d', mark: 'AC' },
  illustrator: { name: 'Illustrator', role: 'Vẽ minh họa', color: '#3f8178', mark: 'MH' },
  map_agent: { name: 'Map Agent', role: 'Bản đồ & biểu đồ', color: '#8b6e4e', mark: 'BD' },
  reviewer: { name: 'Reviewer', role: 'Kiểm duyệt', color: '#b04e3a', mark: 'KD' },
  layout_editor: { name: 'Layout', role: 'Dàn trang PDF', color: '#5d6b8a', mark: 'DT' },
}

const STAGE_LABELS: Record<string, string> = {
  pm: 'Quản lý', researcher: 'Nghiên cứu', planner: 'Lập kế hoạch', budget: 'Ngân sách',
  content_editor: 'Nội dung', visuals: 'Hình ảnh', reviewer: 'Kiểm duyệt',
  layout_editor: 'Dàn trang', completed: 'Hoàn tất', needs_attention: 'Cần xem xét',
}

const STATUS_LABELS: Record<string, string> = {
  queued: 'Đang chờ', running: 'Đang chạy', stopped: 'Đã dừng', failed: 'Thất bại',
  completed: 'Hoàn tất', paused: 'Đã tạm dừng', needs_attention: 'Cần xem xét',
  pause_requested: 'Đang chờ dừng', awaiting_approval:'Chờ người dùng duyệt', awaiting_input:'Chờ bổ sung yêu cầu',
}
STATUS_LABELS.awaiting_approval = 'Chờ người dùng duyệt'
STATUS_LABELS.awaiting_input = 'Chờ bổ sung yêu cầu'
STATUS_LABELS.pause_requested = 'Yêu cầu tạm dừng'


import { parseRoute, collectPages, taskState, workflowConnectionStatus } from './ui'
import type { Tab } from './ui'
import { Schedule, CrewEditor, EventInspector, ApprovalPanel } from './WorkspaceTabs'
type SystemPage = 'home' | 'models'

function formatTime(value: string) {
  return new Intl.DateTimeFormat('vi-VN', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' }).format(new Date(value))
}

function statusClass(status: string) { return `status status-${status.replace('_', '-')}` }

function agentInfo(id: string, agents: ProjectAgent[] = []) {
  const profile = agents.find(item => item.enabled && (item.role_key === id || item.id === id))
  if (profile) return { name: profile.name, role: AGENTS[profile.role_key]?.role || profile.role_key, color: profile.color, mark: profile.name.slice(0, 2).toUpperCase() }
  return AGENTS[id] || { name: id, role: id, color: '#5d655f', mark: 'AI' }
}

/* ── Components ── */

function AgentAvatar({ id, small = false, agents = [], color }: { id: string; small?: boolean; agents?: ProjectAgent[]; color?: string }) {
  const agent = agentInfo(id, agents)
  return <div className={`avatar ${small ? 'small' : ''}`} style={{ background: color || agent.color }}>{agent.mark}</div>
}

/* ── Home Page ── */
function HomePage({ projects, onCreate, onOpen }: { projects: Project[]; onCreate: () => void; onOpen: (id: string) => void }) {
  const running = projects.filter(p => ['running', 'queued'].includes(p.status)).length
  const completed = projects.filter(p => p.status === 'completed').length
  return <main className="home-page">
    <section className="home-hero">
      <div className="home-copy">
        <p className="eyebrow">TRAVELBOOK STUDIO</p>
        <h1>Lên kế hoạch cho<em>chuyến đi tiếp theo.</em></h1>
        <p className="lede">Từ điểm đến bạn yêu thích đến cẩm nang của riêng bạn, với lịch trình, hình ảnh và ngân sách trong một file PDF.</p>
        <div className="home-hero-actions"><button className="primary large" onClick={onCreate}><Plus size={18} /> Tạo cẩm nang mới<ArrowRight size={17} /></button>
          {projects.length > 0 && <button className="home-continue" onClick={() => onOpen(projects[0].id)}>Mở dự án gần nhất <ArrowRight size={15} /></button>}</div>
      </div>
      <figure className="home-destination"><img src="/images/nha-trang-coast.jpg" alt="Bờ biển và đường Trần Phú tại Nha Trang" />
        <figcaption><span className="home-destination-name"><MapPin size={15} /> Nha Trang, Việt Nam</span><small>Ảnh <a href="https://commons.wikimedia.org/wiki/File:Nhatrang_rue_tran_phu.jpg" target="_blank" rel="noreferrer">ntt</a> · <a href="https://creativecommons.org/licenses/by-sa/3.0" target="_blank" rel="noreferrer">CC BY-SA 3.0</a></small></figcaption>
      </figure>
    </section>
    {projects.length > 0 && <section className="home-projects">
      <div className="home-section-head"><div><h2>Các chuyến đi của bạn</h2></div><div className="home-stats"><span><b>{running}</b> đang chạy</span><span><b>{completed}</b> hoàn tất</span></div></div>
      <div className="home-project-grid">{projects.slice(0, 12).map((project, index) => <button className="home-project-card" key={project.id} style={{ '--trip-color': ['#35796e', '#b3774d', '#607bb0', '#8a6d96'][index % 4] } as React.CSSProperties} onClick={() => onOpen(project.id)}>
        <div className="home-card-top"><div className="project-thumb"><BookOpen size={17} /></div><span className={statusClass(project.status)}><i />{STATUS_LABELS[project.status] || project.status}</span></div>
        <h3>{project.title}</h3><p>{project.prompt}</p>
        <div className="home-progress"><i style={{ width: `${project.progress}%` }} /></div>
        <div className="home-card-foot"><span>{STAGE_LABELS[project.stage] || project.stage}</span><strong>{project.progress}%</strong><ArrowRight size={15} /></div>
      </button>)}</div>
    </section>}
  </main>
}

/* ── New Project Modal ── */
function NewProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Project) => void }) {
  const [title, setTitle] = useState('Dự án cẩm nang du lịch')
  const [prompt, setPrompt] = useState('')
  const [autoStart, setAutoStart] = useState(true)
  const [humanApproval, setHumanApproval] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    if (prompt.trim().length < 10) return setError('Hãy mô tả yêu cầu rõ hơn (ít nhất 10 ký tự).')
    setBusy(true); setError('')
    try {
      const result = await api.create(title.trim(), prompt.trim(), autoStart, humanApproval)
      onCreated(result)
    } catch (e) { setError(e instanceof Error ? e.message : 'Không thể tạo dự án') } finally { setBusy(false) }
  }

  return <div className="modal-backdrop" onMouseDown={e => e.target === e.currentTarget && onClose()}>
    <section className="modal project-modal">
      <button className="icon-btn close" onClick={onClose}><X size={20} /></button>
      <p className="eyebrow">NEW PROJECT</p>
      <h2>Bạn muốn đi đâu?</h2>
      <p>Mô tả chuyến đi. Hệ thống sẽ tự động khởi chạy quy trình multi-agent, nghiên cứu và xuất PDF.</p>
      <input value={title} onChange={e => setTitle(e.target.value)} placeholder="Tên dự án" className="title-input" />
      <textarea autoFocus maxLength={6000} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Ví dụ: Lên cẩm nang Đà Nẵng 3 ngày cho 2 người, ngân sách 5 triệu đồng, thích biển và ẩm thực…" />
      <div className="crew-actions"><label><input type="checkbox" checked={autoStart} onChange={e=>setAutoStart(e.target.checked)}/> Chạy ngay sau khi tạo</label><label><input type="checkbox" checked={humanApproval} onChange={e=>setHumanApproval(e.target.checked)}/> Tôi muốn duyệt từng giai đoạn</label></div><div className="prompt-meta"><span>{prompt.length}/6000</span></div>
      {error && <div className="form-error"><AlertTriangle size={16} />{error}</div>}
      <button className="primary full" disabled={busy} onClick={submit}>{busy ? <RefreshCw className="spin" size={18} /> : <Sparkles size={18} />}{busy ? 'Đang khởi chạy dự án…' : 'Bắt đầu'}<ArrowRight size={18} /></button>
    </section>
  </div>
}

/* ── Confirm Delete ── */
function ConfirmDeleteModal({ project, busy, onClose, onConfirm }: { project: ProjectDetail; busy: boolean; onClose: () => void; onConfirm: () => void }) {
  return <div className="modal-backdrop" onMouseDown={e => e.target === e.currentTarget && !busy && onClose()}>
    <section className="modal confirm-modal">
      <button className="icon-btn close" disabled={busy} onClick={onClose}><X size={20} /></button>
      <div className="danger-icon"><Trash2 size={24} /></div>
      <p className="eyebrow">DELETE PROJECT</p>
      <h2>Xóa dự án này?</h2>
      <p>"{project.title}" cùng toàn bộ dữ liệu và file sẽ bị xóa vĩnh viễn.</p>
      <div className="modal-actions">
        <button className="secondary" disabled={busy} onClick={onClose}>Giữ lại</button>
        <button className="danger-button" disabled={busy} onClick={onConfirm}>{busy ? <RefreshCw className="spin" size={17} /> : <Trash2 size={17} />} Xóa vĩnh viễn</button>
      </div>
    </section>
  </div>
}

/* ── Sidebar ── */
function Sidebar({ projects, selected, systemPage, onSelect, onCreate, onHome, onModels }: {
  projects: Project[]; selected?: string; systemPage: SystemPage;
  onSelect: (id: string) => void; onCreate: () => void; onHome: () => void; onModels: () => void
}) {
  return <aside className="sidebar">
    <button className="brand home-link" onClick={onHome} title="Về trang chủ"><div className="brand-mark">TB</div><div><strong>TravelBook</strong><span>AGENT STUDIO</span></div></button>
    <button className="primary new-button" onClick={onCreate}><Plus size={17} /> Dự án mới</button>
    <div className="side-label">HỆ THỐNG</div>
    <button className={`system-nav ${systemPage === 'models' ? 'active' : ''}`} onClick={onModels}>
      <Settings2 size={17} /><span><strong>Model 9Router</strong><small>Cấu hình model</small></span><ChevronRight size={14} />
    </button>
    <div className="side-label">DỰ ÁN ({projects.length})</div>
    <div className="project-list">{projects.map(project => <button key={project.id} className={`project-item ${selected === project.id ? 'active' : ''}`} onClick={() => onSelect(project.id)}>
      <div className="project-thumb"><BookOpen size={17} /></div>
      <div><strong>{project.title}</strong><span>{STAGE_LABELS[project.stage] || project.stage} · {project.progress}%</span></div>
      <ChevronRight size={15} />
    </button>)}</div>
  </aside>
}

/* ── Topbar ── */
function Topbar({ project, tab, setTab, busy, onRetry, onPause, onResume, onStop, onDelete, onHome }: {
  project: ProjectDetail; tab: Tab; setTab: (t: Tab) => void; busy: boolean;
  onRetry: () => void; onPause: () => void; onResume: () => void; onStop: () => void; onDelete: () => void; onHome: () => void
}) {
  const tabs: { id: Tab; label: string; icon: typeof Activity }[] = [
    { id: 'overview', label: 'Tổng quan', icon: LayoutDashboard },
    { id: 'list', label: 'List', icon: ListTodo },
    { id: 'schedule', label: 'Lịch', icon: CalendarDays },
    { id: 'agents', label: 'Agent', icon: Users },
    { id: 'messages', label: 'Giao tiếp', icon: MessageSquareText },
    { id: 'artifacts', label: 'Sản phẩm', icon: GalleryVerticalEnd },
  ]
  return <><header className="topbar">
    <div className="top-title"><button className="project-home-button" onClick={onHome} title="Về trang chủ"><LayoutDashboard size={18} /></button><div><div className="crumb">DỰ ÁN <ChevronRight size={13} /> {project.id.slice(0, 12).toUpperCase()}</div><h1>{project.title}</h1></div></div>
    <div className="top-actions">
      {['failed', 'needs_attention', 'stopped', 'queued'].includes(project.status) && <button className="resume-button" disabled={busy} onClick={onRetry}>{busy ? <RefreshCw className="spin" size={15} /> : <Play size={15} />} Tiếp tục</button>}
      {project.status === 'paused' && <button className="resume-button" disabled={busy} onClick={onResume}><Play size={15} /> Tiếp tục</button>}
      {['running', 'queued'].includes(project.status) && <button className="pause-button" disabled={busy} onClick={onPause}>{busy ? <RefreshCw className="spin" size={15} /> : <Pause size={14} fill="currentColor" />} Tạm dừng</button>}
      {project.status === 'pause_requested' && <button className="pause-button" disabled><RefreshCw className="spin" size={15} /> Yêu cầu tạm dừng</button>}
      {['running', 'queued', 'pause_requested'].includes(project.status) && <button className="stop-button" disabled={busy} onClick={onStop} title="Dừng hẳn dự án"><Square size={13} fill="currentColor" /> Dừng hẳn</button>}
      {/* Nút xóa hiển thị cho queued, paused, completed, failed, stopped - chỉ ẩn khi đang running thực sự */}
      {project.status !== 'running' && project.status !== 'pause_requested' && <button className="delete-button" disabled={busy} onClick={onDelete} title="Xóa dự án"><Trash2 size={16} /></button>}
      <div className="top-status"><span className={statusClass(project.status)}><i />{STATUS_LABELS[project.status] || project.status}</span><strong>{project.progress}%</strong></div>
    </div>
  </header><nav className="tabs">{tabs.map(item => <button className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)} key={item.id}><item.icon size={16} />{item.label}</button>)}</nav></>
}

/* ── Progress Hero ── */
const WORKFLOW_NODES = [
  { key: 'pm', agent: 'pm', x: 104, y: 54, dependencies: [] },
  { key: 'researcher', agent: 'researcher', x: 308, y: 54, dependencies: ['pm'] },
  { key: 'planner', agent: 'planner', x: 512, y: 54, dependencies: ['researcher'] },
  { key: 'budget', agent: 'budget', x: 716, y: 54, dependencies: ['planner'] },
  { key: 'content_editor', agent: 'content_editor', x: 920, y: 54, dependencies: ['budget'] },
  { key: 'image_curator', agent: 'image_curator', x: 308, y: 208, dependencies: ['content_editor'] },
  { key: 'illustrator', agent: 'illustrator', x: 512, y: 208, dependencies: ['content_editor'] },
  { key: 'map_agent', agent: 'map_agent', x: 716, y: 208, dependencies: ['content_editor'] },
  { key: 'reviewer', agent: 'reviewer', x: 308, y: 362, dependencies: ['image_curator', 'illustrator', 'map_agent'] },
  { key: 'layout_editor', agent: 'layout_editor', x: 512, y: 362, dependencies: ['reviewer'] },
  { key: 'pm_finalize', agent: 'pm', x: 716, y: 362, dependencies: ['layout_editor'] },
] as const

const WORKFLOW_STATUS: Record<string, string> = {
  todo: 'Chờ thực hiện', ready: 'Sẵn sàng', in_progress: 'Đang chạy', testing: 'Đang kiểm tra',
  done: 'Hoàn tất', blocked: 'Bị chặn', awaiting_approval: 'Chờ duyệt', paused: 'Tạm dừng', stopped: 'Đã dừng',
}

function AgentWorkflow({ project }: { project: ProjectDetail }) {
  const markerId = useId().replaceAll(':', '')
  const running = ['running', 'queued', 'pause_requested'].includes(project.status)
  const nodes = WORKFLOW_NODES.map(node => {
    const task = project.tasks.find(item => !item.parent_id && item.item_type !== 'subtask' &&
      (item.step_key === node.key || item.id.endsWith(`:${node.key}`)))
    const children = task ? project.tasks.filter(item => item.parent_id === task.id) : []
    const current = children.find(item => ['in_progress', 'testing', 'awaiting_approval'].includes(item.status)) || task
    let status = current?.status || 'todo'
    if (['in_progress', 'testing'].includes(status) && ['paused', 'stopped'].includes(project.status)) status = project.status
    return { ...node, task, status }
  })
  const edges = nodes.flatMap(target => {
    const sources = target.task ? nodes.filter(source => source.task && target.task!.depends_on.includes(source.task.id))
      : nodes.filter(source => (target.dependencies as readonly string[]).includes(source.key))
    return sources.flatMap(source => {
      const status = workflowConnectionStatus(source.status, target.status, project.status)
      if (!status) return []
      const path = source.y === target.y
        ? `M ${source.x + 86} ${source.y} H ${target.x - 90}`
        : `M ${source.x} ${source.y + 32} V ${(source.y + target.y) / 2} H ${target.x} V ${target.y - 36}`
      return [{ key: `${source.key}-${target.key}`, path, status }]
    })
  })
  return <div className="agent-workflow" aria-label="Sơ đồ luồng xử lý của các agent">
    <div className="workflow-canvas">
      <svg className="workflow-arrows" viewBox="0 0 1024 416" aria-hidden="true">
        <defs>{['idle', 'done', 'active'].map(status => <marker key={status} id={`${markerId}-${status}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 1 1 L 9 5 L 1 9 Z" className={`arrowhead ${status}`} /></marker>)}</defs>
        {edges.sort((a, b) => Number(a.status === 'active') - Number(b.status === 'active')).map(edge => <g key={edge.key} className={`workflow-edge ${edge.status}`}>
          <path d={edge.path} markerEnd={`url(#${markerId}-${edge.status})`} />
          {edge.status === 'active' && <path className="workflow-flow" d={edge.path} />}
        </g>)}
      </svg>
      <ol className="workflow-nodes">{nodes.map(node => <li key={node.key} className={`workflow-agent ${node.status} ${running ? 'is-live' : ''}`}
        style={{ left: `${node.x / 10.24}%`, top: `${node.y / 4.16}%`, '--agent': AGENTS[node.agent].color } as React.CSSProperties}
        title={node.task?.title || AGENTS[node.agent].role}>
        <AgentAvatar id={node.agent} small agents={project.agents} color={AGENTS[node.agent].color} />
        <div><strong>{node.key === 'pm_finalize' ? 'PM xác nhận' : agentInfo(node.agent, project.agents).role}</strong><span>{WORKFLOW_STATUS[node.status] || node.status}</span></div>
        {node.status === 'done' ? <Check size={14} className="workflow-node-status" /> : ['in_progress', 'testing'].includes(node.status) && running ? <RefreshCw size={13} className="workflow-node-status spin" /> : <Circle size={8} className="workflow-node-status" />}
      </li>)}</ol>
    </div>
  </div>
}

function ProgressHero({ project }: { project: ProjectDetail }) {
  const active = project.tasks.find(t => t.status === 'in_progress')
  return <section className="progress-hero">
    <div className="progress-copy"><p className="eyebrow">{project.state.fixture_mode?'DEMO JSON':'LIVE AGENTS'}</p><h2>{project.status === 'completed' ? 'Đã xuất bản nháp cẩm nang.' : project.status === 'failed' ? 'Quy trình đã dừng.' : project.status === 'awaiting_input' ? 'PM đang chờ bạn trả lời.' : project.status === 'paused' ? 'Dự án đã tạm dừng.' : 'Hệ thống đang xử lý.'}</h2>
      <p>{active ? `${agentInfo(active.agent,project.agents).name} đang xử lý "${active.title}".` : project.error || 'Đang đồng bộ trạng thái.'}</p>
      <div className="progress-track"><div style={{ width: `${project.progress}%` }} /></div>
      <div className="progress-labels"><span>{STAGE_LABELS[project.stage] || project.stage}</span><span>{project.progress}%</span></div>
    </div>
    <AgentWorkflow project={project} />
  </section>
}

/* ── Agent Grid ── */
function AgentGrid({ tasks, agents }: { tasks: Task[]; agents: ProjectAgent[] }) {
  return <section><div className="section-title"><div><p className="eyebrow">THE CREW</p><h3>Tiến độ từng agent</h3></div><span>{Object.keys(AGENTS).length} agent</span></div>
    <div className="agent-grid">{(Object.keys(AGENTS) as AgentId[]).map(id => {
      const agentTasks = tasks.filter(t => t.agent === id)
      const current = agentTasks.find(t => t.status === 'in_progress') || agentTasks.at(-1)
      const done = agentTasks.filter(t => t.status === 'done').length
      const pct = agentTasks.length ? Math.round(done / agentTasks.length * 100) : 0
      const visualStatus = current?.status || 'todo'
      return <article className={`agent-card ${visualStatus}`} key={id} style={{ '--agent': AGENTS[id].color } as React.CSSProperties}>
        <div className="agent-card-head"><AgentAvatar id={id} agents={agents} color={AGENTS[id].color} /><span className={statusClass(visualStatus)}>{visualStatus === 'in_progress' ? 'Đang làm' : visualStatus === 'done' ? 'Xong' : 'Chờ'}</span></div>
        <h4>{agentInfo(id,agents).name}</h4><p>{AGENTS[id].role}</p><div className="mini-progress"><i style={{ width: `${pct}%` }} /></div>
        <small>{current?.title || 'Chờ PM giao task'}</small>
      </article>
    })}</div>
  </section>
}

/* ── Activity Feed ── */
function ActivityFeed({ events, compact = false }: { events: EventItem[]; compact?: boolean }) {
  const shown = compact ? events.slice(-7).reverse() : events.slice().reverse()
  return <div className={`activity-feed ${compact ? 'compact' : ''}`}>{shown.map(event => <div className="event-row" key={event.id}>
    <AgentAvatar id={event.agent} small /><div className="event-body"><div><strong>{agentInfo(event.agent).name}</strong><time>{formatTime(event.created_at)}</time></div><p>{event.message}</p><span className="event-kind">{event.event_type.replaceAll('_', ' ')}</span></div>
  </div>)}{shown.length === 0 && <div className="empty-list">Chưa có tin nhắn.</div>}</div>
}

/* ── Overview Tab ── */
function Overview({ project, events }: { project: ProjectDetail; events: EventItem[] }) {
  const finished = project.tasks.filter(t => t.status === 'done').length
  return <div className="page-grid"><div className="main-col"><ProgressHero project={project} /><AgentGrid tasks={project.tasks} agents={project.agents||[]} /></div><aside className="right-col">
    <div className="metric-grid"><div><span>WORK ITEMS</span><strong>{project.tasks.length}</strong><small>{finished} hoàn tất</small></div><div><span>PROGRESS</span><strong>{project.progress}%</strong><small>{STAGE_LABELS[project.stage] || project.stage}</small></div></div>
    <section className="side-panel"><div className="panel-head"><h3>Luồng trực tiếp</h3><Activity size={17} /></div><ActivityFeed events={events} compact /></section>
    <section className="brief-card"><span>BRIEF GỐC</span><p>"{project.prompt}"</p></section>
    {project.trip_request && <section className="brief-card"><span>YÊU CẦU</span><p>{project.trip_request.destination.value} · {project.trip_request.days.value} ngày · {Number(project.trip_request.budget.value).toLocaleString('vi-VN')} {project.trip_request.currency.value} · {project.trip_request.travelers.value} người</p></section>}
  </aside></div>
}

/* ── Task List Tab ── */
type WorkState = 'todo' | 'ready' | 'in_progress' | 'blocked' | 'done' | 'testing' | 'awaiting_approval'
const WORK_STATE_LABELS: Record<WorkState, string> = { testing: 'Đang kiểm tra', awaiting_approval: 'Chờ duyệt', todo: 'Chờ', ready: 'Sẵn sàng', in_progress: 'Đang làm', blocked: 'Bị chặn', done: 'Hoàn tất' }

function TaskList({ project }: { project: ProjectDetail }) {
  const { tasks } = project
  const [filter, setFilter] = useState<'all' | WorkState>('all')
  const [search, setSearch] = useState('')
  const roots = tasks.filter(task => !task.parent_id)

  // Ưu tiên trạng thái thực của task từ backend!
  function workState(task: Task): WorkState { return taskState(task, tasks) as WorkState }

  const counts = (['ready', 'in_progress', 'blocked', 'testing', 'awaiting_approval', 'done'] as WorkState[]).reduce((r, s) => ({ ...r, [s]: roots.filter(t => workState(t) === s || childTasks(t, tasks).some(child => workState(child) === s)).length }), {} as Record<WorkState, number>)
  const normalizedSearch = search.trim().toLocaleLowerCase('vi')
  const visible = roots.filter(task => {
    const children = childTasks(task, tasks)
    const matchesText = !normalizedSearch || [task.title, task.detail, agentInfo(task.agent,project.agents).name,
      ...taskDetails(task), ...children.flatMap(child => [child.title, child.detail])].some(v => v?.toLocaleLowerCase('vi').includes(normalizedSearch))
    return matchesText && (filter === 'all' || workState(task) === filter || children.some(child => workState(child) === filter))
  })
  const groups = (['in_progress', 'testing', 'awaiting_approval', 'ready', 'blocked', 'done'] as WorkState[]).map(s => ({ state: s, tasks: visible.filter(t => workState(t) === s) })).filter(g => g.tasks.length > 0)

  return <section className="workspace task-list-workspace">
    <div className="workspace-title"><div><p className="eyebrow">LIST + WORKFLOW</p><h2>Danh sách công việc</h2></div><span><Check size={13} /> {roots.filter(t => t.status === 'done').length}/{roots.length} hoàn tất</span></div>
    <div className="workflow-pipeline"><div className="pipeline-head"><div><strong>Luồng sản xuất</strong><span>PM tự động điều phối</span></div><span className={statusClass(project.status)}><i />{STATUS_LABELS[project.status] || project.status}</span></div>
      <div className="pipeline-track">{roots.map((task, index) => {
        const state = workState(task)
        return <div className="pipeline-unit" key={task.id}>{index > 0 && <i className={`pipeline-link ${state}`} />}<button className={`pipeline-node ${state}`} onClick={() => setFilter(state)} title={task.title}><span>{task.status === 'done' ? <Check size={13} /> : state === 'in_progress' ? <RefreshCw className="spin" size={12} /> : <Circle size={10} />}</span><div><strong>{agentInfo(task.agent,project.agents).role}</strong><small>{WORK_STATE_LABELS[state]}</small></div></button></div>
      })}</div>
    </div>
    <div className="list-toolbar"><label><Search size={15} /><input value={search} onChange={e => setSearch(e.target.value)} placeholder="Tìm công việc, nội dung con hoặc agent…" /></label><div className="state-filters"><button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>Tất cả <b>{roots.length}</b></button>{(['ready', 'in_progress', 'blocked', 'testing', 'awaiting_approval', 'done'] as WorkState[]).map(s => <button className={filter === s ? `active ${s}` : s} onClick={() => setFilter(s)} key={s}>{WORK_STATE_LABELS[s]} <b>{counts[s]}</b></button>)}</div></div>
    <div className="task-list-head"><span>Công việc / người phụ trách</span><span>Trạng thái</span><span>Phụ thuộc</span></div>
    <div className="workflow-list-groups">{groups.map(group => <section className={`workflow-state-group ${group.state}`} key={group.state}><header><div><i /><strong>{WORK_STATE_LABELS[group.state]}</strong><b>{group.tasks.length}</b></div></header><div className="task-list-groups">{group.tasks.map(task => {
      const state = workState(task)
      const children = childTasks(task, tasks)
      const details = taskDetails(task)
      return <article className={`task-list-group ${state} ${task.parent_id ? "subtask-item" : ""}`} key={task.id} style={{ '--agent': agentInfo(task.agent,project.agents).color } as React.CSSProperties}>
        <div className="task-main-row">
          <div className="task-main-info"><AgentAvatar id={task.agent} /><div><span className="task-key">{task.id.toUpperCase()}</span><h3>{task.parent_id ? "↳ " : ""}{task.title}</h3><p>{task.detail}</p><small>{agentInfo(task.agent,project.agents).name} · {agentInfo(task.agent,project.agents).role}{task.depends_on.length > 0 ? ` · Phụ thuộc ${task.depends_on.join(', ')}` : ''}</small></div></div>
          <span className={statusClass(state)}><i />{WORK_STATE_LABELS[state]}</span>
          <span className="task-deps">{task.depends_on.length > 0 ? task.depends_on.length + ' task' : '—'}</span>
        </div>
        {children.length > 0 ? <ul className="task-children">{children.map(child => {
          const childState = workState(child)
          return <li key={child.id} className="task-child"><div className="task-child-copy"><span className="task-child-marker">{childState === 'done' ? <Check size={14} /> : childState === 'in_progress' ? <RefreshCw size={13} className="spin" /> : <Circle size={9} />}</span><div><strong>{child.title}</strong>{child.detail && <p>{child.detail}</p>}</div></div><span className={statusClass(childState)}>{WORK_STATE_LABELS[childState]}</span></li>
        })}</ul> : details.length > 0 && <ul className="task-children task-detail-list" aria-label={`Nội dung công việc của ${agentInfo(task.agent,project.agents).name}`}>{details.map(detail => <li className="task-child" key={detail}><div className="task-child-copy"><span className="task-child-marker"><span className="task-detail-dot" /></span><span>{detail}</span></div></li>)}</ul>}
      </article>
    })}</div></section>)}{visible.length === 0 && <div className="empty-list">Không có công việc phù hợp bộ lọc.</div>}</div>
  </section>
}

/* ── Messages Tab ── */
function Messages({ events, project, onChanged, onLoadMore, hasMore }: { events: EventItem[]; project: ProjectDetail; onChanged: () => Promise<void>; onLoadMore?: () => void; hasMore?: boolean }) {
  const reversed = events.slice().reverse()
  return <section className="workspace messages">
    <div className="workspace-title"><div><p className="eyebrow">AGENT COMMS</p><h2>Nhật ký giao tiếp</h2></div><span>{events.length} sự kiện</span></div>
    <ApprovalPanel project={project} onChanged={onChanged}/><div className="activity-feed message-feed">{reversed.map(event => <div className="event-row message-event" key={event.id}>
      <AgentAvatar id={event.agent} small /><div className="event-body"><div><strong>{agentInfo(event.agent).name} · {agentInfo(event.agent).role}</strong><time>{formatTime(event.created_at)}</time></div><p>{event.message}</p><span className="event-kind">{event.event_type.replaceAll('_', ' ')}</span>
        <EventInspector key={event.id} project={project} event={event} onChanged={onChanged}/>{event.payload && Object.keys(event.payload).length > 0 && <details className="payload-details"><summary>Chi tiết payload</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}
      </div>
    </div>)}{reversed.length === 0 && <div className="empty-list">Chưa có tin nhắn.</div>}
    {hasMore && <div style={{ textAlign: 'center', padding: '16px' }}><button className="secondary" onClick={onLoadMore}>Tải thêm sự kiện</button></div>}
    </div>
  </section>
}

/* ── Router Models Page ── */
export default function App() {
  const [route,setRoute]=useState(()=>parseRoute(window.location.hash))
  const selected=route.id, tab=route.tab, systemPage=route.page
  const [projects,setProjects]=useState<Project[]>([]),[detail,setDetail]=useState<ProjectDetail>()
  const [events,setEvents]=useState<EventItem[]>([]),[artifacts,setArtifacts]=useState<Artifact[]>([])
  const [modal,setModal]=useState(false),[deleteModal,setDeleteModal]=useState(false),[actionBusy,setActionBusy]=useState(false),[error,setError]=useState('')
  const cursor=useRef(0), serial=useRef(0), selectedRef=useRef(selected)
  selectedRef.current=selected
  const refreshList=useCallback(async()=>{try{setProjects(await collectPages(offset=>api.list(100,offset)))}catch(e){setError(e instanceof Error?e.message:String(e))}},[])
  const refreshDetail=useCallback(async()=>{
    if(!selected)return
    const ticket=++serial.current
    try{
      const [proj,art]=await Promise.all([api.get(selected),collectPages(offset=>api.artifacts(selected,100,offset))])
      let after=cursor.current;const added:EventItem[]=[]
      while(true){const page=await api.events(selected,after,100);added.push(...page.items);if(page.next_after_id<=after||page.items.length<100){after=page.next_after_id;break}after=page.next_after_id}
      if(ticket!==serial.current||selected!==selectedRef.current)return
      setDetail(proj);setArtifacts(art);cursor.current=after
      setEvents(previous=>[...new Map([...previous.filter(item=>item.project_id===selected),...added].map(item=>[item.id,item])).values()].sort((a,b)=>a.id-b.id));setError('')
    }catch(e){if(ticket===serial.current)setError(e instanceof Error?e.message:String(e))}
  },[selected])
  useEffect(()=>{const change=()=>setRoute(parseRoute(window.location.hash));window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change)},[])
  useEffect(()=>{refreshList()},[refreshList])
  useEffect(()=>{cursor.current=0;serial.current++;setDetail(undefined);setEvents([]);setArtifacts([]);setDeleteModal(false);refreshDetail()},[selected,refreshDetail])
  useEffect(()=>{const timer=window.setInterval(()=>{refreshList();refreshDetail()},['running','queued','pause_requested'].includes(detail?.status||'')?2500:8000);return()=>window.clearInterval(timer)},[refreshList,refreshDetail,detail?.status])
  function navigate(id?:string,next:Tab='overview',page:'home'|'models'='home'){const hash=page==='models'?'#models':id?`#projects/${id}/${next}`:'#home';window.location.hash=hash;setRoute(parseRoute(hash))}
  const goHome=()=>navigate(),openModels=()=>navigate(undefined,'overview','models'),openProject=(id:string)=>navigate(id)
  const changed=async()=>{await refreshDetail();await refreshList()}
  async function act(action:()=>Promise<unknown>){setActionBusy(true);setError('');try{await action();await changed()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setActionBusy(false)}}
  const handleRetry=()=>detail&&act(()=>api.retry(detail.id)),handlePause=()=>detail&&act(()=>api.pause(detail.id)),handleResume=()=>detail&&act(()=>api.resume(detail.id)),handleStop=()=>detail&&act(()=>api.stop(detail.id))
  async function remove(){if(!detail)return;setActionBusy(true);try{await api.remove(detail.id);setDeleteModal(false);goHome();await refreshList()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setActionBusy(false)}}
  return <div className="app-shell"><Sidebar projects={projects} selected={selected} systemPage={systemPage} onSelect={openProject} onCreate={()=>setModal(true)} onHome={goHome} onModels={openModels}/><div className="app-main">
    {error&&<div role="alert" className="api-error">{error}<button onClick={changed}>Thử lại</button></div>}
    {systemPage==='models'?<RouterModels/>:!selected?<HomePage projects={projects} onCreate={()=>setModal(true)} onOpen={openProject}/>:detail?<>
      <Topbar project={detail} tab={tab} setTab={next=>navigate(selected,next)} busy={actionBusy} onRetry={handleRetry} onPause={handlePause} onResume={handleResume} onStop={handleStop} onDelete={()=>setDeleteModal(true)} onHome={goHome}/>
      <div className="content">{detail.status==='pause_requested'&&<div role="status" className="pause-notice"><strong>Yêu cầu tạm dừng</strong><p>Hệ thống đang hoàn tất bước hoặc nhóm agent hiện tại và ghi checkpoint SQLite. Chỉ có thể tiếp tục sau khi trạng thái chuyển sang Đã tạm dừng.</p></div>}{detail.status==='paused'&&<div role="status" className="pause-notice"><strong>Đã tạm dừng ở bước an toàn</strong><p>Tiến trình đã lưu. Bấm Tiếp tục để chạy từ checkpoint; bạn có thể đóng và mở lại backend.</p></div>}{detail.state.fixture_mode===true&&<div className="demo-notice">Dự án đang dùng đồ thị JSON mẫu; chưa phải dữ liệu du lịch đã xác minh.</div>}
        {detail.error&&<div role="alert" className="workflow-error">{detail.error}</div>}
        <ClarificationPanel key={detail.id} project={detail} onChanged={changed}/>
        {tab==='overview'&&<Overview project={detail} events={events}/>}
        {tab==='list'&&<TaskList project={detail}/>}{tab==='schedule'&&<Schedule tasks={detail.tasks}/>}
        {tab==='agents'&&<CrewEditor project={detail} onChanged={changed} onPause={handlePause}/>}
        {tab==='messages'&&<Messages key={selected} events={events} project={detail} onChanged={changed}/>}
        {tab==='artifacts'&&<Artifacts key={detail.id} artifacts={artifacts} project={detail}/>}
      </div></>:<div className="loader">Đang tải dự án…</div>}
    </div>{modal&&<NewProjectModal onClose={()=>setModal(false)} onCreated={project=>{setModal(false);navigate(project.id);refreshList()}}/>}{deleteModal&&detail&&<ConfirmDeleteModal project={detail} busy={actionBusy} onClose={()=>setDeleteModal(false)} onConfirm={remove}/>}</div>
}
