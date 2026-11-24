import type { Task } from './types'
export const TABS = ['overview','list','schedule','messages','agents','artifacts'] as const
export type Tab = typeof TABS[number]
export function parseRoute(hash: string): {page:'home'|'models';id?:string;tab:Tab} {
  const parts=hash.replace(/^#/,'').split('/')
  if(parts[0]==='models')return {page:'models',tab:'overview'}
  if(parts[0]==='projects'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(parts[1]||''))return {page:'home',id:parts[1],tab:TABS.includes(parts[2] as Tab)?parts[2] as Tab:'overview'}
  return {page:'home',tab:'overview'}
}
export function taskState(task: Task, tasks: Task[]) {
  if(['done','in_progress','blocked','ready','testing','awaiting_approval'].includes(task.status))return task.status
  return task.depends_on.some(id=>tasks.find(item=>item.id===id)?.status!=='done')?'blocked':'ready'
}
export function workflowConnectionStatus(sourceStatus: string, targetStatus: string, projectStatus: string): 'active' | 'done' | 'idle' | null {
  const started = ['in_progress', 'testing', 'done', 'awaiting_approval', 'blocked', 'paused', 'stopped'].includes(targetStatus)
  if (sourceStatus !== 'done' || !started) return null
  if (targetStatus === 'done') return 'done'
  const running = ['running', 'queued', 'pause_requested'].includes(projectStatus)
  return running && ['in_progress', 'testing'].includes(targetStatus) ? 'active' : 'idle'
}
export async function collectPages<T extends {id:string}>(load:(offset:number)=>Promise<{items:T[];total:number}>) {
  const result=new Map<string,T>();let offset=0
  while(true){const page=await load(offset);for(const item of page.items)result.set(item.id,item);offset+=page.items.length;if(!page.items.length||offset>=page.total)break}
  return [...result.values()]
}
