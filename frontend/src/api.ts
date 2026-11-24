import type { Artifact, ArtifactPage, EventPage, Project, ProjectDetail, ProjectPage, RouterCatalog, ProjectAgent, EventContent } from './types'

const ROOT = import.meta.env.VITE_API_URL || ''

async function json<T>(path: string, options?: RequestInit): Promise<T> {
  const url = path.startsWith('http://') || path.startsWith('https://') ? path : `${ROOT}${path}`
  const response = await fetch(url, options)
  if (!response.ok) {
    const raw = await response.text()
    try { const detail = JSON.parse(raw).detail; throw new Error(Array.isArray(detail) ? detail.map((item: {loc?: unknown[];msg?:string}) => `${item.loc?.join('.')}: ${item.msg}`).join('; ') : detail || raw || `HTTP ${response.status}`) }
    catch (error) { if (error instanceof SyntaxError) throw new Error(raw || `HTTP ${response.status}`); throw error }
  }
  if (response.status === 204) return undefined as unknown as T
  return response.json()
}

export const api = {
  addAgent: (id: string, body: Partial<ProjectAgent>) => json<ProjectAgent>(`/api/projects/${id}/agents`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  updateAgent: (id: string, agent: string, body: Partial<ProjectAgent>) => json<ProjectAgent>(`/api/projects/${id}/agents/${agent}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  removeAgent: (id: string, agent: string) => json(`/api/projects/${id}/agents/${agent}`, {method:'DELETE'}),
  approvalSettings: (id: string, enabled: boolean, stages: string[]) => json(`/api/projects/${id}/settings`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({approval_enabled:enabled,approval_stages:stages})}),
  decide: (id: string, approval: string, decision: string, feedback: string, target_agent: string) => json(`/api/projects/${id}/approvals/${approval}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision,feedback,target_agent})}),
  eventContent: (id: string, event: number) => json<EventContent>(`/api/projects/${id}/events/${event}/content`),
  editContent: (id: string, event: number, content: unknown) => json(`/api/projects/${id}/events/${event}/content`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})}),
  intervene: (id: string, event_id: number, target_agent: string, feedback: string) => json(`/api/projects/${id}/interventions`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id,target_agent,feedback})}),
  health: () => json<{ status: string }>('/health'),

  models: () => json<RouterCatalog>('/api/router/models'),
  syncModels: () => json<RouterCatalog>('/api/router/models/sync', {method:'POST'}),

  updateModels: (models: string[], image_model: string, image_fallback_models: string[] = []) =>
    json<RouterCatalog>('/api/router/models', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ models, image_model, image_fallback_models }),
    }),

  list: (limit = 50, offset = 0) =>
    json<ProjectPage>(`/api/projects?limit=${limit}&offset=${offset}`),

  get: (id: string) => json<ProjectDetail>(`/api/projects/${id}`),

  create: (title: string, prompt: string, start_workflow = true, approval_enabled = false) =>
    json<ProjectDetail>('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, prompt, start_workflow, approval_enabled }),
    }),

  remove: (id: string) => json<void>(`/api/projects/${id}`, { method: 'DELETE' }),

  retry: (id: string) => json<{ status: string }>(`/api/projects/${id}/retry`, { method: 'POST' }),

  pause: (id: string) => json<{ status: string }>(`/api/projects/${id}/pause`, { method: 'POST' }),

  resume: (id: string) => json<{ status: string }>(`/api/projects/${id}/resume`, { method: 'POST' }),
  answer: (id:string,answer:string) => json(`/api/projects/${id}/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer})}),

  stop: (id: string) => json<{ status: string }>(`/api/projects/${id}/stop`, { method: 'POST' }),

  events: (id: string, afterId = 0, limit = 100) =>
    json<EventPage>(`/api/projects/${id}/events?after_id=${afterId}&limit=${limit}`),

  artifacts: (id: string, limit = 50, offset = 0) =>
    json<ArtifactPage>(`/api/projects/${id}/artifacts?limit=${limit}&offset=${offset}`),

  artifact: (id: string, artifactId: string) =>
    json<Artifact & { download_url: string }>(`/api/projects/${id}/artifacts/${artifactId}`),

  downloadUrl: (url: string) => {
    if (!url) return ''
    if (url.startsWith('http://') || url.startsWith('https://')) return url
    return `${ROOT}${url}`
  },
  previewUrl: (url: string) => {
    const target = new URL(url.startsWith('http://') || url.startsWith('https://') ? url : `${ROOT}${url}`, window.location.origin)
    target.searchParams.set('inline', 'true')
    return target.toString()
  },
}
