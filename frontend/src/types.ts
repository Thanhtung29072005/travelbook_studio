export type AgentId =
  | 'pm' | 'researcher' | 'planner' | 'budget' | 'content_editor'
  | 'image_curator' | 'illustrator' | 'map_agent' | 'reviewer' | 'layout_editor'

export interface Project {
  id: string; title: string; prompt: string; status: string; stage: string; progress: number
  pause_requested: boolean; final_pdf: string | null; error: string | null
  created_at: string; updated_at: string
}

export interface ProjectPage {
  items: Project[]; limit: number; offset: number; total: number
}

export interface Task {
  id: string; title: string; agent: string; status: string
  depends_on: string[]; detail: string; order_index: number
  parent_id?: string; item_type?: 'task' | 'subtask'; step_key?: string
  start_at?: string; due_at?: string
}

export interface EventItem {
  id: number; project_id: string; agent: string; event_type: string
  message: string; payload: Record<string, unknown>; created_at: string
}

export interface EventPage {
  items: EventItem[]; next_after_id: number
}

export interface Artifact {
  id: string; project_id: string; agent_id: string; kind: string; name: string
  path: string; mime_type: string; source: string; confidence: number
  created_at: string; download_url: string
}

export interface ArtifactPage {
  items: Artifact[]; limit: number; offset: number; total: number
}

export interface TripRequest {
  destination: { value: string; source: string; confidence: number }
  days: { value: number; source: string; confidence: number }
  budget: { value: string; source: string; confidence: number }
  currency: { value: string; source: string; confidence: number }
  travelers: { value: number; source: string; confidence: number }
  start_date?: { value: string; source: string; confidence: number } | null
  interests?: { value: string; source: string; confidence: number }[]
}

export interface ProjectDetail extends Project {
  trip_request: TripRequest | null
  state: Record<string, unknown>
  tasks: Task[]
  agents: ProjectAgent[]
  approvals: Approval[]
  approval_enabled: boolean
  approval_stages: string[]
}

export interface ProjectAgent {
  id: string; name: string; role_key: AgentId; instructions: string; color: string; enabled: boolean; is_core: boolean
  model: string | null; fallback_models: string[]; image_model: string | null; temperature: number | null
}
export interface Approval { id: string; stage: string; node: string; status: string; content: Record<string, unknown>; feedback: string; allowed_targets?: AgentId[]; target_agent?: AgentId }
export interface EventContent { event: EventItem; content: Record<string, unknown>; content_key: string; artifacts: Artifact[]; editable: boolean; can_intervene: boolean; note: string }

export interface RouterCatalog {
  router: string
  api_key_configured: boolean
  models: string[]
  available_models: string[]
  image_model: string
  image_fallback_models?: string[]
  synced_at?: string | null
  catalog_error?: string | null
}
