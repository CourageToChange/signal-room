import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import pressureDropRaw from './demo/generated/pressure-drop.json'
import { DrillExperience } from './DrillExperience'
import type { DrillScenario } from './types'
import './styles.css'

const pressureDrop = pressureDropRaw as unknown as DrillScenario

createRoot(document.getElementById('root')!).render(
  <StrictMode><DrillExperience scenario={pressureDrop} /></StrictMode>,
)
