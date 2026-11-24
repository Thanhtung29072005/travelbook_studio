import { useEffect, useId, useRef, useState } from 'react'
import { Download, Eye, FileText, GalleryVerticalEnd, X } from 'lucide-react'
import { api } from './api'
import { artifactLabel, artifactLibrary } from './artifactLibrary'
import type { Artifact, ProjectDetail } from './types'

const CATEGORIES: Record<string, string> = { photo: 'Ảnh địa điểm', illustration: 'Minh họa', map: 'Bản đồ', chart: 'Biểu đồ' }

function ArtifactPreview({ artifact, onClose }: { artifact: Artifact; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const [error, setError] = useState(false)
  const pdf = artifact.mime_type === 'application/pdf'
  useEffect(() => {
    const dialog = dialogRef.current!
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog.showModal()
    return () => { dialog.close(); document.body.style.overflow = previousOverflow }
  }, [])
  return <dialog ref={dialogRef} className={`asset-preview-dialog ${pdf ? 'is-pdf' : ''}`} aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose() }} onClick={event => { if (event.target === event.currentTarget) onClose() }}>
    <div className="asset-preview-shell">
      <header><div><h2 id={titleId}>{artifactLabel(artifact)}</h2><p>{artifact.name}</p></div><div className="asset-preview-actions">
        <a className="secondary" href={api.downloadUrl(artifact.download_url)} download><Download size={15} />Tải xuống</a>
        <button autoFocus className="icon-btn" onClick={onClose} aria-label="Đóng xem trước"><X size={21} /></button>
      </div></header>
      <div className="asset-preview-content">{error ? <p role="alert">Chưa tải được nội dung xem trước. Bạn có thể tải file bằng nút phía trên.</p>
        : pdf ? <iframe title={`Xem trước ${artifact.name}`} src={api.previewUrl(artifact.download_url)} onError={() => setError(true)} />
          : <img src={api.previewUrl(artifact.download_url)} alt={artifactLabel(artifact)} onError={() => setError(true)} />}</div>
    </div>
  </dialog>
}

export function Artifacts({ artifacts, project }: { artifacts: Artifact[]; project: ProjectDetail }) {
  const [selected, setSelected] = useState<Artifact>()
  const [category, setCategory] = useState('all')
  const library = artifactLibrary(artifacts, project)
  const categories = Object.entries(CATEGORIES).filter(([key]) => library.images.some(item => item.kind === key))
  const effectiveCategory = categories.some(([key]) => key === category) ? category : 'all'
  const images = library.images.filter(item => effectiveCategory === 'all' || item.kind === effectiveCategory)
  return <section className="workspace output-workspace">
    <div className="workspace-title"><div><h2>Cẩm nang & hình ảnh</h2><p className="output-subtitle">Xem trước sản phẩm và tải về khi cần.</p></div></div>
    {library.pdf ? <article className="pdf-feature">
      <button className="pdf-feature-cover" onClick={() => setSelected(library.pdf)} aria-label="Xem trước cẩm nang PDF"><FileText size={43} /><span>PDF</span></button>
      <div className="pdf-feature-copy"><span>CẨM NANG HIỆN TẠI</span><h3>{project.title}</h3><p>{library.pdf.name}</p></div>
      <div className="pdf-feature-actions"><button className="primary" onClick={() => setSelected(library.pdf)}><Eye size={16} />Xem PDF</button>
        <a href={api.downloadUrl(library.pdf.download_url)} download><Download size={15} />Tải PDF</a></div>
    </article> : <div className="output-pdf-pending"><FileText size={26} /><div><strong>Chưa có cẩm nang PDF</strong><p>File PDF sẽ xuất hiện tại đây sau bước dàn trang.</p></div></div>}
    {library.images.length > 0 && <section className="output-gallery" aria-label="Thư viện ảnh và bản đồ">
      <div className="output-gallery-header"><h3>Hình ảnh & bản đồ <span>{library.images.length}</span></h3>
        {categories.length > 1 && <div className="output-filters" role="group" aria-label="Lọc loại hình ảnh"><button aria-pressed={effectiveCategory === 'all'} onClick={() => setCategory('all')}>Tất cả</button>{categories.map(([key, label]) => <button key={key} aria-pressed={effectiveCategory === key} onClick={() => setCategory(key)}>{label}</button>)}</div>}
      </div>
      <div className="artifact-grid">{images.map(item => <article className="artifact-card output-image-card" key={item.id}>
        <button className="output-image-preview" onClick={() => setSelected(item)} aria-label={`Xem trước ${artifactLabel(item)}`}><img loading="lazy" src={api.previewUrl(item.download_url)} alt={artifactLabel(item)} /><span><Eye size={15} />Xem trước</span></button>
        <div className="output-image-info"><div><small>{CATEGORIES[item.kind]}</small><h4>{artifactLabel(item)}</h4></div><a href={api.downloadUrl(item.download_url)} download aria-label={`Tải ${artifactLabel(item)}`} title="Tải xuống"><Download size={17} /></a></div>
      </article>)}</div>
    </section>}
    {artifacts.length === 0 && <div className="output-empty"><GalleryVerticalEnd size={28} /><p>Ảnh, bản đồ và biểu đồ sẽ xuất hiện khi các agent tạo xong.</p></div>}
    {library.internal.length > 0 && <details className="output-internal"><summary>Tệp dữ liệu & phiên bản trước <span>{library.internal.length}</span></summary><ul>{library.internal.map(item => <li key={item.id}><FileText size={16} /><span><strong>{item.name}</strong><small>{item.kind.replaceAll('_', ' ')} · {new Date(item.created_at).toLocaleString('vi-VN')}</small></span>
      {(item.mime_type === 'application/pdf' || item.mime_type.startsWith('image/')) && <button onClick={() => setSelected(item)}>Xem trước</button>}<a href={api.downloadUrl(item.download_url)} download aria-label={`Tải ${item.name}`}><Download size={15} /></a>
    </li>)}</ul></details>}
    {selected && <ArtifactPreview key={selected.id} artifact={selected} onClose={() => setSelected(undefined)} />}
  </section>
}
