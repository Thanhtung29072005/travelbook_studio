// Local disposable UI test harness; never used by the application entrypoint.
import {useEffect,useState} from 'react'
import {createRoot} from 'react-dom/client'
import {CrewEditor} from './CrewManagement'
import {RouterModels} from './ModelManagement'
import type {ProjectDetail} from './types'
import './styles.css'
function Preview(){const [project,setProject]=useState<ProjectDetail>(),[models,setModels]=useState(false)
  async function reload(){setProject(await (await fetch('/api/projects/ui-test')).json())}
  useEffect(()=>{reload()},[])
  return <main style={{padding:32}}><p>UI TEST — dữ liệu dùng thử, API mô phỏng, không gọi LLM.</p><button onClick={()=>setModels(false)}>Agent</button><button onClick={()=>setModels(true)}>Model</button>{models?<RouterModels/>:project?<CrewEditor project={project} onChanged={reload}/>:<p>Đang tải</p>}</main>}
createRoot(document.getElementById('root')!).render(<Preview/> )
