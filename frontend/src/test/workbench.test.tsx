/**
 * The workbench's safety claims, asserted on rendered output.
 *
 * These are the tests that would catch the UI quietly lying. Each one corresponds to a
 * claim the workbench makes to a reviewer:
 *
 *   - an ephemeral store is *said* to be ephemeral;
 *   - a self-declared identity is *said* to be self-declared;
 *   - a stale run is visible and cannot be reviewed;
 *   - amounts arrive and render as the exact text the API sent;
 *   - there is no control that moves money.
 *
 * The last one is asserted by looking for the absence of a button. A test that read the
 * component's props would pass while the UI still rendered an "Approve" button, because
 * the button would be hardcoded.
 */

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { ApiError, api } from '../api/client'
import type { Capabilities, CaseDetail } from '../api/types'

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

function investigation(overrides: Partial<CaseDetail['investigations'][number]> = {}) {
  return {
    id: 'run-1',
    dispute_id: 'case-1',
    version: 1,
    status: 'COMPLETE',
    evidence_fingerprint: 'sha256:'.concat('a'.repeat(64)),
    is_stale: false,
    created_at: '2026-03-31T12:00:00Z',
    summary: 'The invoice overstates usage.',
    provider_name: 'mock',
    model: 'mock-deterministic-v1',
    prompt_version: 'prompt-v1',
    engine_version: '1.0.0',
    evidence: [
      {
        natural_key: 'invoice:INV-1',
        evidence_type: 'INVOICE',
        content_hash: 'sha256:' + 'b'.repeat(64),
      },
    ],
    calculation: {
      currency: 'USD',
      engine_version: '1.0.0',
      recorded_total: '28.40',
      recalculated_total: '5021.86',
      difference: '4993.46',
      outstanding: '5021.86',
      is_complete: true,
      is_provisional: false,
      unresolved_metrics: [],
      trace: { rules: [] },
    },
    findings: [
      {
        id: 'finding-1',
        code: 'OVERAGE_TIER_MISMATCH',
        severity: 'HIGH',
        category: 'pricing',
        narrative: 'The invoice used the wrong tier for the billed volume.',
        confidence: '0.9',
        supporting_evidence: ['invoice:INV-1'],
        reviews: [],
      },
    ],
    hypotheses: [],
    resolution_options: [
      {
        id: 'option-1',
        option_type: 'ISSUE_CREDIT',
        title: 'Credit the difference',
        narrative: '',
        rationale: 'The recalculated total is lower than the invoice.',
        requires_human_approval: true,
        supporting_evidence: ['invoice:INV-1'],
        reviews: [],
      },
    ],
    degradations: [],
    ...overrides,
  }
}

function caseDetail(overrides: Partial<CaseDetail> = {}): CaseDetail {
  return {
    id: 'case-1',
    external_id: 'DSC-000123',
    invoice_external_id: 'INV-2026-03-0042',
    contract_external_id: 'CTR-5512',
    status: 'AWAITING_REVIEW',
    severity: 'MEDIUM',
    description: 'We were billed the wrong rate for API calls.',
    created_at: '2026-03-31T12:00:00Z',
    version: 1,
    is_stale: false,
    evidence_fingerprint: 'sha256:' + 'a'.repeat(64),
    current_investigation_version: 1,
    review_count: 0,
    evidence: [
      {
        natural_key: 'invoice:INV-2026-03-0042',
        evidence_type: 'INVOICE',
        content_hash: 'sha256:' + 'b'.repeat(64),
      },
    ],
    investigations: [investigation()],
    reviews: [],
    source_document: {},
    ...overrides,
  }
}

function caseSummary(overrides: Partial<CaseDetail> = {}) {
  const detail = caseDetail(overrides)
  return {
    id: detail.id,
    external_id: detail.external_id,
    invoice_external_id: detail.invoice_external_id,
    contract_external_id: detail.contract_external_id,
    status: detail.status,
    severity: detail.severity,
    description: detail.description,
    created_at: detail.created_at,
    version: detail.version,
    is_stale: detail.is_stale,
    evidence_fingerprint: detail.evidence_fingerprint,
    current_investigation_version: detail.current_investigation_version,
    review_count: detail.reviews.length,
  }
}

/**
 * Reads the value of a <dt>/<dd> figure by its label.
 *
 * The labels are wrapped in a <div> inside the <dl>, which is valid HTML5 grouping but
 * is not reflected in jsdom's accessibility tree, so the roles resolve to nothing and
 * the two elements have to be related through the DOM instead.
 */
function figure(label: string): string | null {
  return screen.getByText(label).parentElement?.querySelector('dd')?.textContent ?? null
}

function stubApi(overrides: Partial<Record<keyof typeof api, unknown>> = {}) {
  const defaults = {
    capabilities: vi.fn().mockResolvedValue(CAPABILITIES),
    listCases: vi
      .fn()
      .mockResolvedValue({ items: [caseSummary()], total: 1, limit: 50, offset: 0 }),
    getCase: vi.fn().mockResolvedValue(caseDetail()),
    investigate: vi.fn().mockResolvedValue(investigation()),
    recordReview: vi.fn().mockResolvedValue({
      id: 'review-1',
      investigation_id: 'run-1',
      target_type: 'FINDING',
      target_id: 'finding-1',
      action: 'ACCEPT',
      actor_id: 'dev-reviewer',
      actor_role: 'reviewer',
      rationale: 'ok',
      evidence_fingerprint_seen: 'sha256:' + 'a'.repeat(64),
      created_at: '2026-03-31T12:00:00Z',
    }),
    reopen: vi.fn().mockResolvedValue(caseDetail()),
    attachEvidence: vi.fn().mockResolvedValue(caseDetail()),
  }
  for (const [name, value] of Object.entries({ ...defaults, ...overrides })) {
    vi.spyOn(api, name as keyof typeof api).mockImplementation(value as never)
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the capabilities notice', () => {
  it('says the store is not durable', async () => {
    stubApi()
    render(<App />)

    expect(await screen.findByText(/not durable/i)).toBeInTheDocument()
  })

  it('says the identity is self-declared', async () => {
    stubApi()
    render(<App />)

    expect(await screen.findByText(/identity is self-declared/i)).toBeInTheDocument()
  })

  it('says nothing about durability when the store is postgres', async () => {
    stubApi({ capabilities: vi.fn().mockResolvedValue({ ...CAPABILITIES, store: 'postgres', persistence_is_durable: true }) })
    render(<App />)

    await screen.findByText(/dispute cases/i)
    expect(screen.queryByText(/not durable/i)).not.toBeInTheDocument()
  })
})

describe('the queue', () => {
  it('defaults to cases awaiting review', async () => {
    const listCases = vi.fn().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 })
    stubApi({ listCases })
    render(<App />)

    await screen.findByText(/dispute cases/i)
    expect(listCases).toHaveBeenCalledWith({ status: 'AWAITING_REVIEW', limit: 50 })
  })

  it('omits the status filter entirely for "all", rather than sending undefined', async () => {
    // A query string of `status=undefined` would be silently ignored by the backend and
    // the UI would show an unfiltered list while claiming to be filtered.
    const listCases = vi.fn().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 })
    stubApi({ listCases })
    render(<App />)

    await screen.findByText(/dispute cases/i)
    await userEvent.click(screen.getByRole('button', { name: 'All' }))

    await waitFor(() => expect(listCases).toHaveBeenCalledWith({ limit: 50 }))
  })

  it('surfaces a database outage as retryable rather than as a bug', async () => {
    stubApi({
      listCases: vi
        .fn()
        .mockRejectedValue(
          new ApiError(503, 'DATABASE_UNAVAILABLE', 'The database is not reachable.', 'r1', {
            retryable: true,
          }),
        ),
    })
    render(<App />)

    expect(await screen.findByText(/database is unavailable/i)).toBeInTheDocument()
  })
})

describe('the case detail view', () => {
  it('renders the amounts exactly as the API sent them', async () => {
    stubApi()
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await screen.findByText('Recalculation')

    /*
     * Grouped, but not rounded, not re-scaled and not re-ordered. A reviewer
     * reconciling against an invoice must see the stored digits, including the scale:
     * the recorded total is stored as "28.40" and must not be shown as "28.4".
     *
     * Each figure is read through its own label rather than by text alone, because the
     * recalculated total and the outstanding balance are both "USD 5,021.86" here.
     */
    expect(figure('Invoiced')).toBe('USD 28.40')
    expect(figure('Recalculated')).toBe('USD 5,021.86')
    // Unsigned, because the stored difference carries no sign and deciding whether an
    // overcharge is favourable or adverse is the reviewer's call, not the formatter's.
    expect(figure('Difference')).toBe('USD 4,993.46')
    expect(figure('Outstanding')).toBe('USD 5,021.86')
  })

  it('shows a provisional calculation as provisional', async () => {
    stubApi({
      getCase: vi.fn().mockResolvedValue(
        caseDetail({
          investigations: [
            investigation({
              calculation: {
                currency: 'USD',
                engine_version: '1.0.0',
                recorded_total: '28.40',
                recalculated_total: '5000.00',
                difference: '4971.60',
                is_complete: false,
                is_provisional: true,
                unresolved_metrics: ['api_calls'],
                trace: {},
              },
            }),
          ],
        }),
      ),
    })
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))

    expect(await screen.findByText('Provisional')).toBeInTheDocument()
    expect(screen.getByText(/could not be priced/i)).toBeInTheDocument()
  })

  it('shows the run provenance', async () => {
    stubApi()
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))

    expect(await screen.findByText('mock-deterministic-v1')).toBeInTheDocument()
    expect(screen.getByText('prompt-v1')).toBeInTheDocument()
  })

  it('warns when the newest run no longer describes the case', async () => {
    stubApi({
      getCase: vi.fn().mockResolvedValue(
        caseDetail({
          is_stale: true,
          status: 'REOPENED',
          investigations: [investigation({ is_stale: true })],
        }),
      ),
    })
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))

    expect(await screen.findByText(/no longer describes this case/i)).toBeInTheDocument()
  })

  it('refuses to let a stale finding be reviewed', async () => {
    // The UI must not offer the action, because the backend will refuse it and a
    // reviewer who typed a rationale first deserves better than a 409.
    stubApi({
      getCase: vi.fn().mockResolvedValue(
        caseDetail({
          is_stale: true,
          status: 'REOPENED',
          investigations: [investigation({ is_stale: true })],
        }),
      ),
    })
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await screen.findByText(/no longer describes this case/i)

    expect(screen.getByText(/re-run the investigation before reviewing/i)).toBeInTheDocument()

    /*
     * Asserted on the controls rather than on the Record button alone. An empty
     * rationale also disables Record, so a button-only assertion would pass even if the
     * staleness check were deleted -- it would be proving the wrong thing. These
     * assertions fail if staleness stops disabling the form at all.
     */
    expect(screen.getAllByLabelText('Rationale')[0]).toBeDisabled()
    expect(screen.getAllByLabelText('Review action')[0]).toBeDisabled()
    expect(screen.getAllByRole('button', { name: 'Record' })[0]).toBeDisabled()

    // And nothing was posted while the reviewer was working.
    expect(api.recordReview).not.toHaveBeenCalled()
  })

  it('records a review with the rationale it was given', async () => {
    stubApi()
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await userEvent.type(await screen.findByLabelText('Rationale'), 'matches the export')
    await userEvent.click(screen.getAllByRole('button', { name: 'Record' })[0]!)

    await waitFor(() =>
      expect(api.recordReview).toHaveBeenCalledWith('case-1', {
        investigation_id: 'run-1',
        target_type: 'FINDING',
        target_id: 'finding-1',
        action: 'ACCEPT',
        rationale: 'matches the export',
      }),
    )
  })

  it('will not submit an AMEND with no replacement wording', async () => {
    // An amend with no text records a decision that says nothing.
    stubApi()
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await userEvent.selectOptions(await screen.findByLabelText('Review action'), 'AMEND')

    expect(screen.getAllByRole('button', { name: 'Record' })[0]).toBeDisabled()
  })

  it('explains a stale refusal in the reviewers terms', async () => {
    stubApi({
      recordReview: vi
        .fn()
        .mockRejectedValue(
          new ApiError(
            409,
            'INVALID_TRANSITION',
            'investigation is stale; reopen or re-investigate before recording a review',
            'r2',
            null,
          ),
        ),
    })
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await userEvent.type(await screen.findByLabelText('Rationale'), 'checked')
    await userEvent.click(screen.getAllByRole('button', { name: 'Record' })[0]!)

    expect(await screen.findByText(/that investigation is stale/i)).toBeInTheDocument()
  })

  it('lists every evidence key a finding may cite', async () => {
    stubApi()
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))

    const table = await screen.findByRole('table')
    expect(within(table).getByText('invoice:INV-2026-03-0042')).toBeInTheDocument()
  })

  it('marks a resolution option as needing approval and offers no control for it', async () => {
    stubApi()
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))

    expect(await screen.findByText(/needs human approval/i)).toBeInTheDocument()
    expect(screen.getByText(/does not carry out this step/i)).toBeInTheDocument()
  })
})

describe('what the workbench does not offer', () => {
  beforeEach(() => {
    stubApi()
  })

  it('has no approve, adjust or execute control anywhere', async () => {
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await screen.findByText(/recalculation/i)

    const labels = [
      ...screen.queryAllByRole('button'),
      ...screen.queryAllByRole('link'),
      ...screen.queryAllByRole('menuitem'),
    ].map((element) => element.textContent?.toLowerCase() ?? '')

    for (const forbidden of ['approve', 'adjust', 'execute', 'issue credit', 'refund', 'pay']) {
      expect(labels.filter((label) => label.includes(forbidden))).toEqual([])
    }
  })

  it('offers APPROVE as a review verb', async () => {
    // The backend has no such verb, and the select is built from the API's list, so
    // adding it here means adding it to a schema that must not have it yet.
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await screen.findByText(/recalculation/i)

    const options = within(await screen.findByLabelText('Review action')).getAllByRole('option')

    expect(options.map((option) => option.textContent)).toEqual([
      'Accept',
      'Reject',
      'Request more info',
      'Amend wording',
    ])
  })

  it('does not offer reopen on a case that is not closed', async () => {
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    await screen.findByText(/recalculation/i)

    expect(screen.getByText(/reopen is available once the case is closed/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reopen' })).not.toBeInTheDocument()
  })

  it('requires a reason before reopening', async () => {
    stubApi({
      getCase: vi.fn().mockResolvedValue(caseDetail({ status: 'RESOLVED' })),
    })
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /DSC-000123/ }))
    const button = await screen.findByRole('button', { name: 'Reopen' })

    expect(button).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/reason for reopening/i), 'usage export')
    expect(button).toBeEnabled()
  })
})
