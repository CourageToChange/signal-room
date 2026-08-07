import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import pressureDropRaw from './demo/generated/pressure-drop.json'
import type { DrillScenario } from './types'
import { DrillExperience } from './DrillExperience'

const scenario = pressureDropRaw as unknown as DrillScenario

afterEach(() => vi.useRealTimers())

describe('Pressure Drop drill', () => {
  it('moves from a private brief to evidence and a scored assessment', () => {
    render(<DrillExperience scenario={scenario} />)
    expect(screen.getByRole('heading', { name: 'Pressure Drop' })).toBeInTheDocument()
    expect(screen.getByText(/No login, cookies, analytics/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Start incident drill/i }))
    fireEvent.click(screen.getByRole('button', { name: /Skip to incident/i }))
    expect(screen.getByText(/Simulation · no live systems/i)).toBeInTheDocument()
    expect(screen.getByText('0s ago')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Make the call/i })).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Orchid Guest'))
    fireEvent.click(screen.getByLabelText("Inspect the guest's memory trend and recent changes"))
    fireEvent.click(screen.getByRole('button', { name: /Submit assessment/i }))
    expect(screen.getByText('Incident understood')).toBeInTheDocument()
    expect(screen.getByText(/2\/2 decisions/)).toBeInTheDocument()
  })

  it('supports playback controls, dismissal, debrief, restart, and embedded exit', () => {
    const exit = vi.fn()
    render(<DrillExperience scenario={scenario} embedded onExit={exit} />)
    fireEvent.click(screen.getByRole('button', { name: /Start incident drill/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(screen.getByRole('button', { name: 'Play' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Speed'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Skip to incident' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: 'Open assessment' }))
    const reopened = screen.getByRole('dialog')
    fireEvent.click(within(reopened).getByLabelText('Atlas Node'))
    fireEvent.click(within(reopened).getByLabelText('Delete old data immediately'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit assessment' }))
    expect(screen.getByText('Debrief complete')).toBeInTheDocument()
    expect(screen.getByText(/0\/2 decisions/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Run drill again' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Exit drill' }))
    expect(exit).toHaveBeenCalledOnce()
  })

  it('traps focus in the assessment and reaches the replay state at the bounded duration', () => {
    vi.useFakeTimers()
    render(<DrillExperience scenario={scenario} />)
    fireEvent.click(screen.getByRole('button', { name: /Start incident drill/i }))
    fireEvent.change(screen.getByLabelText('Speed'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Skip to incident' }))
    const dialog = screen.getByRole('dialog')
    const focusable = [...dialog.querySelectorAll<HTMLElement>('button, input, select, textarea, [href], [tabindex]:not([tabindex="-1"])')].filter((item) => !item.hasAttribute('disabled'))
    focusable[0].focus()
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(focusable.at(-1))
    focusable.at(-1)?.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(focusable[0])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close assessment' }))
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(screen.getByRole('button', { name: 'Replay' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Replay' }))
    vi.useRealTimers()
  })

  it('derives safe brief defaults without mapped assets or an incident', () => {
    const frame = scenario.frames[0]
    render(<DrillExperience scenario={{ ...scenario, frames: [{ ...frame, snapshot: { ...frame.snapshot, assets: [], incidents: [] } }], duration_seconds: 20 }} />)
    expect(screen.getByText('0 assets')).toBeInTheDocument()
    expect(screen.getByText('20 seconds')).toBeInTheDocument()
  })
})
