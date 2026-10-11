/**
 * The open-dispute flow, asserted on rendered output.
 *
 * Opening a case is the one place the workbench sends a write the customer never
 * confirmed, so the claims worth pinning down are: the form is reachable from the
 * shell, an incomplete payload never reaches the API, and a valid payload is sent
 * exactly as entered -- with an idempotency key, so a retry cannot open twins.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { api } from '../api/client'
import type { Capabilities, CaseDetail, CaseSummary, OpenDisputeRequest } from '../api/types'

const CAPABILITIES: Capabilities = {
  llm_provider: 'mock',
  llm_model: 'mock-deterministic-v1',
  prompt_version: 'prompt-v1',
  store: 'memory',
  persistence_is_durable: false,
  identity: {
    mode: 'demo-header',
    is_authentication: false,
    actor_id: 'dev-reviewer',
    actor_role: 'reviewer',
  },
  features: {},
}

function caseSummary(): CaseSummary {
  return {
    id: 'case-1',
    external_id: 'DSC-000123',
    invoice_external_id: 'INV-2026-03-0042',
    contract_external_id: 'CTR-5512',
    status: 'AWAITING_REVIEW',
    severity: 'MEDIUM',
    is_stale: false,
    evidence_fingerprint: 'sha256:' + 'a'.repeat(64),
    version: 1,
    created_at: '2026-03-31T12:00:00Z',
    investigation_count: 1,
    current_investigation_version: 1,
    outstanding: '5021.86',
    currency: 'USD',
    finding_count: 1,
  }
}

function caseDetail(): CaseDetail {
  return {
    id: 'case-1',
    external_id: 'DSC-000999',
    invoice_external_id: 'INV-1',
    contract_external_id: 'CTR-1',
    status: 'OPEN',
    severity: 'MEDIUM',
    is_stale: false,
    evidence_fingerprint: 'sha256:' + 'a'.repeat(64),
    version: 1,
    created_at: '2026-03-31T12:00:00Z',
    investigation_count: 0,
    current_investigation_version: null,
    outstanding: '12480.00',
    currency: 'USD',
    finding_count: 0,
    description: '',
    evidence: [],
    current_investigation: null,
    investigations: [],
    reviews: [],
    decisions: [],
  }
}

function stubApi(overrides: Partial<Record<keyof typeof api, unknown>> = {}) {
  const defaults = {
    capabilities: vi.fn().mockResolvedValue(CAPABILITIES),
    listCases: vi.fn().mockResolvedValue({ items: [caseSummary()], total: 1, limit: 50, offset: 0 }),
    getCase: vi.fn().mockResolvedValue(caseDetail()),
    getAudit: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 }),
    createDispute: vi.fn().mockResolvedValue(caseDetail()),
  }
  for (const [name, value] of Object.entries({ ...defaults, ...overrides })) {
    vi.spyOn(api, name as keyof typeof api).mockImplementation(value as never)
  }
}

/** The submit button shares its name with the shell's nav item, so it is the last one. */
function submitButton(): HTMLElement {
  const buttons = screen.getAllByRole('button', { name: 'Open dispute' })
  return buttons[buttons.length - 1]!
}

async function openForm() {
  stubApi()
  render(<App />)
  await screen.findByText(/dispute cases/i)
  await userEvent.click(screen.getByRole('button', { name: 'Open dispute' }))
  await screen.findByText('Open a dispute')
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the open-dispute flow', () => {
  it('is reachable from the shell', async () => {
    await openForm()

    expect(screen.getByLabelText('Dispute reference')).toBeInTheDocument()
    expect(screen.getByLabelText('Contract reference')).toBeInTheDocument()
    expect(screen.getByLabelText('Invoice reference')).toBeInTheDocument()
  })

  it('refuses to send an incomplete payload', async () => {
    stubApi()
    render(<App />)
    await screen.findByText(/dispute cases/i)
    await userEvent.click(screen.getByRole('button', { name: 'Open dispute' }))
    await screen.findByText('Open a dispute')

    await userEvent.click(submitButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(/need attention/i)
    expect(api.createDispute).not.toHaveBeenCalled()
  })

  it('sends the records exactly as entered, with an idempotency key', async () => {
    stubApi()
    render(<App />)
    await screen.findByText(/dispute cases/i)
    await userEvent.click(screen.getByRole('button', { name: 'Open dispute' }))
    await screen.findByText('Open a dispute')

    await userEvent.type(screen.getByLabelText('Dispute reference'), 'DSC-000999')
    await userEvent.type(screen.getByLabelText('Contract reference'), 'CTR-1')
    fireEvent.change(screen.getByLabelText('Dispute text'), {
      target: { value: 'Billed the wrong rate' },
    })
    await userEvent.type(screen.getByLabelText('Invoice reference'), 'INV-1')
    fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-03-01' } })
    fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-03-31' } })
    await userEvent.type(screen.getByLabelText('Stated total'), '12480.00')
    await userEvent.type(screen.getByLabelText('Metric key 1'), 'api_calls')
    await userEvent.type(screen.getByLabelText('Recorded amount'), '12480.00')

    await userEvent.click(submitButton())

    const expected: OpenDisputeRequest = {
      dispute_external_id: 'DSC-000999',
      contract_external_id: 'CTR-1',
      dispute_text: 'Billed the wrong rate',
      severity: 'MEDIUM',
      description: '',
      invoice: {
        external_id: 'INV-1',
        currency: 'USD',
        period_start: '2026-03-01',
        period_end: '2026-03-31',
        stated_total: { amount: '12480.00', currency: 'USD' },
        lines: [
          {
            metric_key: 'api_calls',
            line_type: 'USAGE',
            recorded_amount: { amount: '12480.00', currency: 'USD' },
          },
        ],
      },
    }

    await waitFor(() => expect(api.createDispute).toHaveBeenCalledTimes(1))
    const [payload, key] = vi.mocked(api.createDispute).mock.calls[0]!
    expect(payload).toEqual(expected)
    expect(typeof key).toBe('string')
    expect(key.length).toBeGreaterThan(0)

    // Success lands on the case it opened, rather than leaving the reviewer on the form.
    expect(await screen.findByText('Invoice', { selector: 'dt' })).toBeInTheDocument()
  })
})
