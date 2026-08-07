import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { incident } from '../test/fixtures'
import { IncidentPanel } from './IncidentPanel'

describe('IncidentPanel', () => {
  it('supports selection, acknowledgement, notes, evidence, and runbooks', async () => {
    const select = vi.fn()
    const acknowledge = vi.fn().mockResolvedValue(undefined)
    const addNote = vi.fn().mockResolvedValue(undefined)
    render(<IncidentPanel incidents={[incident]} selectedId={incident.id} canMutate referenceTime="2026-07-15T17:59:25Z" onSelect={select} onAcknowledge={acknowledge} onAddNote={addNote} />)
    expect(screen.getByText('25s ago')).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('list', { name: 'Incident list' })).getByRole('listitem'))
    expect(select).toHaveBeenCalledWith(incident.id)
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge incident' }))
    await waitFor(() => expect(acknowledge).toHaveBeenCalledWith(incident.id))
    fireEvent.change(screen.getByLabelText('Private responder note'), { target: { value: '  Checked memory  ' } })
    fireEvent.submit(screen.getByRole('button', { name: 'Add to timeline' }).closest('form')!)
    await waitFor(() => expect(addNote).toHaveBeenCalledWith(incident.id, 'Checked memory'))
    expect(screen.getByText('Failure threshold confirmed')).toBeInTheDocument()
    expect(screen.getByText('Guest pressure')).toBeInTheDocument()
  })

  it('renders empty and resolved states, including retained notes and close', () => {
    const { rerender } = render(<IncidentPanel incidents={[]} selectedId={null} canMutate onSelect={() => undefined} />)
    expect(screen.getByText('No active incidents')).toBeInTheDocument()
    const close = vi.fn().mockResolvedValue(undefined)
    const resolved = { ...incident, state: 'resolved' as const, acknowledged_at: '2026-07-15T18:01:00Z', notes: [{ id: 1, incident_id: incident.id, created_at: '2026-07-15T18:02:00Z', author: 'Owner', body: 'Capacity recovered' }] }
    rerender(<IncidentPanel incidents={[resolved]} selectedId="missing" canMutate onSelect={() => undefined} onClose={close} />)
    fireEvent.click(screen.getByRole('button', { name: 'Close resolved incident' }))
    expect(close).toHaveBeenCalledWith(incident.id)
    expect(screen.getByText('Capacity recovered')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Acknowledge incident' })).not.toBeInTheDocument()
  })
})
