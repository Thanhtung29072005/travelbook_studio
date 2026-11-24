import type { Artifact, ProjectDetail } from './types'

export function artifactLabel(artifact: Artifact): string {
  if (artifact.mime_type === 'application/pdf') return 'Cẩm nang du lịch PDF'
  const stem = artifact.name.replace(/\.[^.]+$/, '')
  const day = /^day_?(\d+)$/.exec(stem)
  if (day) return `${artifact.kind === 'map' ? 'Bản đồ' : 'Minh họa'} ngày ${day[1]}`
  const labels: Record<string, string> = {
    cover: 'Ảnh bìa', budget: 'Biểu đồ ngân sách', weather: 'Biểu đồ thời tiết', timeline: 'Lịch trình toàn chuyến',
  }
  return labels[stem] || (artifact.kind === 'photo' ? 'Ảnh địa điểm' : artifact.name)
}

export function artifactLibrary(artifacts: Artifact[], project: ProjectDetail) {
  // Retire the six generic decorations in older projects as well as new runs.
  const retained = artifacts.filter(item => !(item.kind === 'illustration' && item.mime_type.startsWith('image/') &&
    /^(?:icon_(?:location|food|transport|culture)|background_(?:waves|dots))\.[^.]+$/i.test(item.name)))
  const sorted = retained.map((artifact, index) => ({ artifact, index })).sort((a, b) =>
    (Date.parse(a.artifact.created_at) || 0) - (Date.parse(b.artifact.created_at) || 0) || a.index - b.index).map(item => item.artifact)
  const final = project.state.final_pdf as { id?: string; path?: string } | undefined
  const pdfs = sorted.filter(item => item.mime_type === 'application/pdf')
  const pdf = pdfs.find(item => item.id === final?.id) || pdfs.find(item => item.path === (project.final_pdf || final?.path)) || pdfs.at(-1)
  const latest = new Map<string, Artifact>()
  const fields: Record<string, string> = { photo: 'photos', illustration: 'illustrations', map: 'maps', chart: 'charts' }
  for (const item of sorted) {
    if (!item.mime_type.startsWith('image/') || !fields[item.kind]) continue
    const current = project.state[fields[item.kind]]
    if (Array.isArray(current) && current.length > 0 && !current.some(asset =>
      asset && typeof asset === 'object' && (asset.id === item.id || asset.path === item.path || asset.local_path === item.path))) continue
    latest.set(`${item.agent_id}:${item.kind}:${item.name}`, item)
  }
  const images = [...latest.values()]
  const visible = new Set([pdf?.id, ...images.map(item => item.id)])
  return { pdf, images, internal: sorted.filter(item => !visible.has(item.id)).reverse() }
}
