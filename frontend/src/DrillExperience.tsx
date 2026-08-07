import { useEffect, useMemo, useRef, useState } from 'react'
import type { DrillScenario, DrillSnapshot, Observation } from './types'
import { ConsoleLayout } from './components/ConsoleLayout'

function snapshotAt(scenario: DrillScenario, elapsed: number): DrillSnapshot {
  return [...scenario.frames].reverse().find((frame) => frame.at_seconds <= elapsed)?.snapshot ?? scenario.frames[0].snapshot
}

function samplesAt(scenario: DrillScenario, assetId: string, elapsed: number): Observation[] {
  return scenario.frames
    .filter((frame) => frame.at_seconds <= elapsed)
    .map((frame) => {
      const state = frame.snapshot.states.find((item) => item.asset_id === assetId)
      return {
        asset_id: assetId,
        observed_at: frame.snapshot.generated_at,
        health: state?.health ?? 'unknown',
        message: state?.message ?? 'Awaiting telemetry',
        latency_ms: state?.latency_ms ?? null,
        cpu_ratio: state?.cpu_ratio ?? null,
        memory_ratio: state?.memory_ratio ?? null,
        disk_ratio: state?.disk_ratio ?? null,
        details: {},
      } satisfies Observation
    })
}

export function DrillExperience({ scenario, embedded = false, onExit }: { scenario: DrillScenario; embedded?: boolean; onExit?: () => void }) {
  const initialAsset = scenario.frames[0]?.snapshot.assets[0]?.id ?? ''
  const incidentMoment = scenario.frames.find((frame) => frame.snapshot.incidents.length > 0)?.at_seconds ?? Math.round(scenario.duration_seconds / 2)
  const [started, setStarted] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [speed, setSpeed] = useState(1)
  const [selectedAsset, setSelectedAsset] = useState(initialAsset)
  const [selectedIncident, setSelectedIncident] = useState<string | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [submitted, setSubmitted] = useState(false)
  const [assessmentDismissed, setAssessmentDismissed] = useState(false)
  const assessmentRef = useRef<HTMLElement>(null)
  const showAssessment = elapsed >= incidentMoment && !assessmentDismissed

  useEffect(() => {
    if (!playing) return
    const timer = window.setInterval(() => {
      setElapsed((current) => {
        const next = Math.min(scenario.duration_seconds, current + 0.25 * speed)
        if (next >= scenario.duration_seconds) setPlaying(false)
        return next
      })
    }, 250)
    return () => window.clearInterval(timer)
  }, [playing, scenario.duration_seconds, speed])

  useEffect(() => {
    if (!showAssessment || !assessmentRef.current) return
    const panel = assessmentRef.current
    const previouslyFocused = document.activeElement as HTMLElement | null
    const focusable = () => [...panel.querySelectorAll<HTMLElement>('button, input, select, textarea, [href], [tabindex]:not([tabindex="-1"])')].filter((item) => !item.hasAttribute('disabled'))
    focusable()[0]?.focus()
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setAssessmentDismissed(true); return }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (!items.length) return
      const first = items[0]
      const last = items.at(-1)!
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    panel.addEventListener('keydown', keydown)
    return () => { panel.removeEventListener('keydown', keydown); previouslyFocused?.focus() }
  }, [showAssessment])

  const snapshot = useMemo(() => snapshotAt(scenario, elapsed), [scenario, elapsed])
  const effectiveIncident = selectedIncident ?? snapshot.incidents[0]?.id ?? null
  const samples = useMemo(() => samplesAt(scenario, selectedAsset, elapsed), [scenario, selectedAsset, elapsed])
  const correct = scenario.questions.filter((question) => answers[question.id] === question.answer).length

  const restart = () => {
    setElapsed(0)
    setPlaying(true)
    setSelectedAsset(initialAsset)
    setSelectedIncident(null)
    setAnswers({})
    setSubmitted(false)
    setAssessmentDismissed(false)
  }

  if (!started) {
    return (
      <main id="main" className="drill-brief">
        <div className="drill-brief__mark" aria-hidden="true"><span /><span /><span /></div>
        <p className="eyebrow">Signal Room field exercise 01</p>
        <h1>{scenario.title}</h1>
        <p className="drill-brief__lead">{scenario.summary}</p>
        <div className="brief-grid">
          <div><strong>{scenario.duration_seconds} seconds</strong><span>Accelerated timeline</span></div>
          <div><strong>{scenario.frames[0]?.snapshot.assets.length ?? 0} assets</strong><span>Dependency map</span></div>
          <div><strong>Zero access</strong><span>Entirely fictional data</span></div>
        </div>
        <div className="brief-instructions">
          <h2>Your task</h2>
          <ol><li>Watch the evidence develop.</li><li>Find the common point of failure.</li><li>Choose the safest first response.</li></ol>
        </div>
        <button className="button button--primary button--large" type="button" onClick={() => { setStarted(true); setPlaying(true) }}>
          Start incident drill
        </button>
        <p className="privacy-note">No login, cookies, analytics, or network API calls.</p>
      </main>
    )
  }

  const controls = (
    <div className="drill-controls" aria-label="Simulation controls">
      <button type="button" onClick={() => setPlaying((value) => !value)}>{playing ? 'Pause' : elapsed >= scenario.duration_seconds ? 'Replay' : 'Play'}</button>
      <label>Speed<select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}><option value={1}>1×</option><option value={4}>4×</option><option value={8}>8×</option></select></label>
      <button type="button" onClick={() => setElapsed(incidentMoment)}>Skip to incident</button>
      <span className="drill-clock">T+{Math.floor(elapsed).toString().padStart(2, '0')}s</span>
      {embedded && onExit && <button type="button" onClick={onExit}>Exit drill</button>}
    </div>
  )

  return (
    <div className={showAssessment ? 'drill-shell assessment-open' : 'drill-shell'}>
      <ConsoleLayout
        snapshot={snapshot}
        selectedAssetId={selectedAsset}
        selectedIncidentId={effectiveIncident}
        samples={samples}
        simulation
        controls={controls}
        onSelectAsset={setSelectedAsset}
        onSelectIncident={setSelectedIncident}
      />
      {showAssessment && (
        <aside ref={assessmentRef} className="assessment" role="dialog" aria-modal="true" aria-labelledby="assessment-title">
          <div className="assessment__head"><div><p className="eyebrow">Responder assessment</p><h2 id="assessment-title">Make the call</h2></div><span>{Object.keys(answers).length}/{scenario.questions.length}</span><button className="assessment__close" type="button" onClick={() => setAssessmentDismissed(true)} aria-label="Close assessment">×</button></div>
          {scenario.questions.map((question) => (
            <fieldset key={question.id} disabled={submitted}>
              <legend>{question.prompt}</legend>
              {question.options.map((option) => (
                <label key={option} className={submitted && option === question.answer ? 'is-correct' : ''}>
                  <input type="radio" name={question.id} value={option} checked={answers[question.id] === option} onChange={() => setAnswers((current) => ({ ...current, [question.id]: option }))} />
                  <span>{option}</span>
                </label>
              ))}
              {submitted && <p className="assessment__explanation">{question.explanation}</p>}
            </fieldset>
          ))}
          {!submitted ? (
            <button className="button button--primary" type="button" disabled={Object.keys(answers).length !== scenario.questions.length} onClick={() => setSubmitted(true)}>Submit assessment</button>
          ) : (
            <div className="assessment__result" role="status"><strong>{correct === scenario.questions.length ? 'Incident understood' : 'Debrief complete'}</strong><span>{correct}/{scenario.questions.length} decisions matched the evidence.</span><button className="button" type="button" onClick={restart}>Run drill again</button></div>
          )}
        </aside>
      )}
      {elapsed >= incidentMoment && assessmentDismissed && <button className="assessment-reopen button button--primary" type="button" onClick={() => setAssessmentDismissed(false)}>Open assessment</button>}
    </div>
  )
}
