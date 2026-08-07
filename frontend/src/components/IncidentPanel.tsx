import { FormEvent, useState } from 'react'
import type { Incident } from '../types'

function relativeTime(value: string, referenceTime = Date.now()): string {
  const seconds = Math.max(0, (referenceTime - new Date(value).getTime()) / 1000)
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}

export function IncidentPanel({
  incidents,
  selectedId,
  canMutate,
  referenceTime,
  onSelect,
  onAcknowledge,
  onAddNote,
  onClose,
}: {
  incidents: Incident[]
  selectedId: string | null
  canMutate: boolean
  referenceTime?: string
  onSelect: (id: string) => void
  onAcknowledge?: (id: string) => Promise<void>
  onAddNote?: (id: string, note: string) => Promise<void>
  onClose?: (id: string) => Promise<void>
}) {
  const [note, setNote] = useState('')
  const selected = incidents.find((incident) => incident.id === selectedId) ?? incidents[0]
  const submitNote = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected || !note.trim() || !onAddNote) return
    await onAddNote(selected.id, note.trim())
    setNote('')
  }
  return (
    <section className="panel incidents" aria-labelledby="incidents-title">
      <div className="panel__heading">
        <div>
          <p className="eyebrow">Triage queue</p>
          <h2 id="incidents-title">Incidents</h2>
        </div>
        <span className="count-chip">{incidents.filter((item) => item.state === 'open').length} open</span>
      </div>
      {incidents.length === 0 ? (
        <div className="empty-state">
          <span aria-hidden="true">✓</span>
          <strong>No active incidents</strong>
          <p>Signal Room is watching for repeated or correlated failures.</p>
        </div>
      ) : (
        <>
          <div className="incident-tabs" role="list" aria-label="Incident list">
            {incidents.map((incident) => (
              <button
                type="button"
                role="listitem"
                key={incident.id}
                className={`${selected?.id === incident.id ? 'is-selected' : ''} severity-${incident.severity}`}
                onClick={() => onSelect(incident.id)}
              >
                <span>{incident.state}</span>
                <strong>{incident.title}</strong>
                <small>{relativeTime(incident.opened_at, referenceTime ? new Date(referenceTime).getTime() : undefined)}</small>
              </button>
            ))}
          </div>
          {selected && (
            <div className="incident-detail">
              <p className="incident-detail__summary">{selected.summary}</p>
              <div className="incident-detail__meta">
                <span>{selected.affected_asset_ids.length} affected assets</span>
                <span>{selected.acknowledged_at ? 'Acknowledged' : 'Awaiting acknowledgement'}</span>
              </div>
              {canMutate && !selected.acknowledged_at && selected.state !== 'resolved' && onAcknowledge && (
                <button className="button button--primary" type="button" onClick={() => void onAcknowledge(selected.id)}>
                  Acknowledge incident
                </button>
              )}
              <ol className="timeline" aria-label="Incident evidence timeline">
                {selected.events.map((event) => (
                  <li key={event.id}>
                    <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time>
                    <span><strong>{event.kind}</strong>{event.message}</span>
                  </li>
                ))}
              </ol>
              {selected.runbook && (
                <details className="runbook">
                  <summary>{selected.runbook.title}</summary>
                  <p>{selected.runbook.summary}</p>
                  <ol>{selected.runbook.checks.map((check) => <li key={check}>{check}</li>)}</ol>
                </details>
              )}
              {canMutate && onAddNote && (
                <form className="note-form" onSubmit={(event) => void submitNote(event)}>
                  <label htmlFor="incident-note">Private responder note</label>
                  <textarea id="incident-note" maxLength={2000} value={note} onChange={(event) => setNote(event.target.value)} />
                  <button className="button" type="submit" disabled={!note.trim()}>Add to timeline</button>
                </form>
              )}
              {selected.notes.length > 0 && (
                <div className="notes"><h3>Responder notes</h3>{selected.notes.map((item) => <p key={item.id}>{item.body}</p>)}</div>
              )}
              {canMutate && selected.state === 'resolved' && onClose && (
                <button className="button" type="button" onClick={() => void onClose(selected.id)}>Close resolved incident</button>
              )}
            </div>
          )}
        </>
      )}
    </section>
  )
}
