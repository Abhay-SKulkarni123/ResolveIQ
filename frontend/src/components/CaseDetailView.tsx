/**
 * The case detail view: what the system concluded, on what, and what a human thinks.
 *
 * The layout follows the reviewer's actual question, which is not "what did the model
 * say" but "can I trust this, and against which evidence". So the order is:
 *
 *   provenance  -> is this a mock or a real model, which rules, which prompt
 *   calculation -> the figures, with an explicit provisional marker
 *   evidence    -> every natural key, so a citation can be checked by eye
 *   findings    -> each with its citations and its reviews
 *   history     -> every run, including superseded ones
 *
 * Superseded runs are shown, not hidden. "Compare with what we saw then" is the reason
 * the backend keeps them, and a UI that only ever renders the newest run makes the
 * history pointless.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api } from '../api/client'
import type {
  CaseDetail,
  FindingResponse,
  HypothesisResponse,
  InvestigationResponse,
  ResolutionOptionResponse,
  ReviewAction,
  ReviewResponse,
} from '../api/types'
import {
  Amount,
  Difference,
  EmptyState,
  ErrorPanel,
  Fingerprint,
  StaleBadge,
  StatusBadge,
} from './primitives'
import { formatMoneyDetail } from './money'

const REVIEW_ACTIONS: { value: ReviewAction; label: string }[] = [
  { value: 'ACCEPT', label: 'Accept' },
  { value: 'REJECT', label: 'Reject' },
  { value: 'REQUEST_MORE_INFO', label: 'Request more info' },
  { value: 'AMEND', label: 'Amend wording' },
]

export function CaseDetailView({
  caseId,
  onChanged,
}: {
  caseId: string
  onChanged?: () => void
}) {
  const [detail, setDetail] = useState<CaseDetail | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setDetail(await api.getCase(caseId))
      setError(null)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, 'UNKNOWN', String(caught), null, null))
    }
  }, [caseId])

  useEffect(() => {
    void load()
  }, [load])

  async function run<T>(action: () => Promise<T>, success: string): Promise<void> {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await action()
      await load()
      onChanged?.()
      setNotice(success)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, 'UNKNOWN', String(caught), null, null))
    } finally {
      setBusy(false)
    }
  }

  if (error && !detail) {
    return <ErrorPanel title="Could not load this case" message={error.message} code={error.code} />
  }
  if (!detail) {
    return <EmptyState>Loading...</EmptyState>
  }

  // The backend names the live run; it does not make the client re-derive it. The old
  // lookup compared a run id against the *case* id, which can never be equal, so it
  // always fell through to "last run wins" and silently mislabelled reopened cases.
  const current = detail.current_investigation
  const latest = current ?? detail.investigations[detail.investigations.length - 1]

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold">{detail.external_id}</h1>
          <StatusBadge status={detail.status} />
          <StaleBadge isStale={detail.is_stale} />
        </div>
        <p className="text-sm text-ink-muted">{detail.description}</p>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-ink-muted">
          <div className="flex gap-1">
            <dt>Invoice</dt>
            <dd className="font-mono">{detail.invoice_external_id}</dd>
          </div>
          {detail.contract_external_id && (
            <div className="flex gap-1">
              <dt>Contract</dt>
              <dd className="font-mono">{detail.contract_external_id}</dd>
            </div>
          )}
          <div className="flex gap-1">
            <dt>Evidence</dt>
            <dd>
              <Fingerprint value={detail.evidence_fingerprint} label="evidence fingerprint" />
            </dd>
          </div>
          <div className="flex gap-1">
            <dt>Version</dt>
            <dd className="font-mono">{detail.version}</dd>
          </div>
        </dl>
      </header>

      {notice && (
        <p role="status" className="rounded border border-line bg-surface px-3 py-2 text-xs text-ink-muted">
          {notice}
        </p>
      )}
      {error && (
        <ErrorPanel
          title={
            error.isStaleEvidence
              ? 'That investigation is stale'
              : error.isRetryable
                ? 'The database is unavailable'
                : 'That action was refused'
          }
          message={error.message}
          code={error.code}
        />
      )}

      <section className="flex flex-wrap gap-2">
        <ActionButton
          busy={busy}
          onClick={() => run(() => api.investigate(detail.id), 'Investigation recorded.')}
          label="Run investigation"
        />
        <ReopenButton
          busy={busy}
          disabled={detail.status !== 'RESOLVED' && detail.status !== 'REJECTED'}
          onReopen={(reason) =>
            run(() => api.reopen(detail.id, reason), 'Case reopened. It needs a new run.')
          }
        />
        {/* No approve, no adjust, no execute. Those arrive with the Phase 8 guards. */}
      </section>

      {detail.is_stale && latest && (
        <div className="rounded border border-warn-border bg-warn-bg px-4 py-3 text-sm text-warn-text">
          <p className="font-medium">The newest run no longer describes this case.</p>
          <p className="mt-1">
            Evidence was attached after version {latest.version} ran, so its findings and
            figures describe records that are no longer on the dispute. Re-run before
            reviewing.
          </p>
        </div>
      )}

      {latest ? (
        <RunView caseId={detail.id} run={latest} onReview={run} busy={busy} />
      ) : (
        <EmptyState>Not investigated yet.</EmptyState>
      )}

      <EvidencePanel detail={detail} />
      <HistoryPanel runs={detail.investigations} currentId={latest?.id ?? null} />
    </div>
  )
}

function ActionButton({
  onClick,
  busy,
  label,
  disabled = false,
}: {
  onClick: () => void
  busy: boolean
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || disabled}
      className="rounded border border-line bg-surface px-3 py-1.5 text-sm font-medium hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {label}
    </button>
  )
}

function ReopenButton({
  onReopen,
  busy,
  disabled,
}: {
  onReopen: (reason: string) => void
  busy: boolean
  disabled: boolean
}) {
  const [reason, setReason] = useState('')
  if (disabled) {
    return (
      <span className="rounded border border-dashed border-line px-3 py-1.5 text-xs text-ink-faint">
        Reopen is available once the case is closed
      </span>
    )
  }
  return (
    <span className="flex items-center gap-2">
      <input
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="Why is this being reopened?"
        aria-label="Reason for reopening"
        className="rounded border border-line px-2 py-1.5 text-sm"
      />
      <ActionButton
        busy={busy}
        disabled={reason.trim() === ''}
        onClick={() => onReopen(reason)}
        label="Reopen"
      />
    </span>
  )
}

function RunView({
  caseId,
  run,
  onReview,
  busy,
}: {
  caseId: string
  run: InvestigationResponse
  onReview: <T>(action: () => Promise<T>, success: string) => Promise<void>
  busy: boolean
}) {
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 border-b border-line pb-2 text-xs text-ink-muted">
        <span>Version {run.version}</span>
        <StaleBadge isStale={run.is_stale} />
        <span>
          Provider <span className="font-mono">{run.provider_name}</span>
        </span>
        <span>
          Model <span className="font-mono">{run.model}</span>
        </span>
        <span>
          Prompt <span className="font-mono">{run.prompt_version}</span>
        </span>
        <span>
          Engine <span className="font-mono">{run.engine_version}</span>
        </span>
        <span>
          Read <Fingerprint value={run.evidence_fingerprint} label="run fingerprint" />
        </span>
      </div>

      {run.degradations.length > 0 && (
        <div className="rounded border border-warn-border bg-warn-bg px-3 py-2 text-xs text-warn-text">
          <p className="font-medium">This run is degraded.</p>
          <ul className="mt-1 list-disc pl-4">
            {run.degradations.map((degradation) => (
              <li key={degradation}>{degradation}</li>
            ))}
          </ul>
          <p className="mt-1">
            Treat its conclusions as incomplete. Nothing here should be relied on without
            checking.
          </p>
        </div>
      )}

      {run.calculation && <CalculationPanel calculation={run.calculation} />}

      <p className="text-sm text-ink">{run.summary}</p>

      {run.findings.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold">Findings</h2>
          {run.findings.map((item) => (
            <FindingCard
              key={item.id}
              caseId={caseId}
              finding={item}
              investigationId={run.id}
              onReview={onReview}
              busy={busy}
              disabled={run.is_stale}
            />
          ))}
        </div>
      )}

      {run.hypotheses.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold">Hypotheses</h2>
          {run.hypotheses.map((item) => (
            <HypothesisCard key={item.id} hypothesis={item} />
          ))}
        </div>
      )}

      {run.resolution_options.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold">Suggested next steps</h2>
          {run.resolution_options.map((item) => (
            <OptionCard key={item.id} option={item} />
          ))}
        </div>
      )}
    </section>
  )
}

function CalculationPanel({
  calculation,
}: {
  calculation: NonNullable<InvestigationResponse['calculation']>
}) {
  return (
    <div className="rounded border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">Recalculation</h2>
        {calculation.is_provisional && (
          <span className="rounded border border-warn-border bg-warn-bg px-2 py-0.5 text-xs text-warn-text">
            Provisional
          </span>
        )}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure label="Invoiced" value={<Amount value={calculation.recorded_total} currency={calculation.currency} />} />
        <Figure label="Recalculated" value={<Amount value={calculation.recalculated_total} currency={calculation.currency} />} />
        <Figure label="Difference" value={<Difference value={calculation.difference ?? null} currency={calculation.currency} />} />
        <Figure label="Outstanding" value={<Amount value={calculation.outstanding ?? null} currency={calculation.currency} />} />
      </dl>

      {calculation.unresolved_metrics.length > 0 && (
        <p className="mt-3 text-xs text-warn-text">
          These metrics could not be priced, so the recalculated figure is a lower
          bound: {calculation.unresolved_metrics.join(', ')}.
        </p>
      )}
      <p className="mt-2 text-xs text-ink-faint">
        Computed by <span className="font-mono">{calculation.engine_version}</span>. These
        figures come from the deterministic engine, not from the model.
      </p>
    </div>
  )
}

function Figure({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-ink-faint">{label}</dt>
      <dd className="mt-0.5 text-sm">{value}</dd>
    </div>
  )
}

function EvidenceChips({ keys }: { keys: string[] }) {
  if (keys.length === 0) {
    return <span className="text-xs text-ink-faint">No citations</span>
  }
  return (
    <ul className="flex flex-wrap gap-1">
      {keys.map((key) => (
        <li
          key={key}
          className="rounded border border-line bg-canvas px-1.5 py-0.5 font-mono text-xs text-ink-muted"
        >
          {key}
        </li>
      ))}
    </ul>
  )
}

function ReviewList({ reviews }: { reviews: ReviewResponse[] }) {
  if (reviews.length === 0) return null
  return (
    <ul className="mt-2 space-y-1 border-l-2 border-line pl-3">
      {reviews.map((review) => (
        <li key={review.id} className="text-xs">
          <span className="font-medium">{review.action.replace('_', ' ').toLowerCase()}</span>{' '}
          <span className="text-ink-muted">
            by {review.actor_id} ({review.actor_role})
          </span>
          {review.rationale && <span className="block text-ink-muted">{review.rationale}</span>}
          {review.amended_narrative && (
            <span className="block text-ink-muted">
              Amended to: {review.amended_narrative}
            </span>
          )}
          <Fingerprint value={review.evidence_fingerprint_seen} label="reviewer saw" />
        </li>
      ))}
    </ul>
  )
}

function FindingCard({
  caseId,
  finding,
  investigationId,
  onReview,
  busy,
  disabled,
}: {
  caseId: string
  finding: FindingResponse
  investigationId: string
  onReview: <T>(action: () => Promise<T>, success: string) => Promise<void>
  busy: boolean
  disabled: boolean
}) {
  return (
    <article className="rounded border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-ink-muted">{finding.code}</span>
        <span className="rounded border border-line px-1.5 py-0.5 text-xs uppercase text-ink-muted">
          {finding.severity}
        </span>
        {finding.confidence !== null && (
          <span className="amount text-xs text-ink-muted">confidence {finding.confidence}</span>
        )}
      </div>
      <p className="mt-2 text-sm">{finding.narrative}</p>
      <div className="mt-2">
        <EvidenceChips keys={finding.supporting_evidence} />
      </div>
      <ReviewList reviews={finding.reviews} />
      <ReviewForm
        busy={busy}
        disabled={disabled}
        {...(disabled
          ? { hint: 'This run is stale. Re-run the investigation before reviewing it.' }
          : {})}
        onSubmit={(action, rationale, amended) =>
          onReview(
            () =>
              api.recordReview(caseId, {
                investigation_id: investigationId,
                target_type: 'FINDING',
                target_id: finding.id,
                action,
                rationale,
                ...(amended ? { amended_narrative: amended } : {}),
              }),
            'Review recorded.',
          )
        }
      />
    </article>
  )
}

function HypothesisCard({ hypothesis }: { hypothesis: HypothesisResponse }) {
  return (
    <article className="rounded border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-ink-muted">{hypothesis.hypothesis_code}</span>
        <span className="rounded border border-line px-1.5 py-0.5 text-xs uppercase text-ink-muted">
          {hypothesis.status}
        </span>
        {hypothesis.likelihood !== null && (
          <span className="amount text-xs text-ink-muted">likelihood {hypothesis.likelihood}</span>
        )}
      </div>
      <p className="mt-2 text-sm font-medium">{hypothesis.title}</p>
      <p className="text-sm text-ink-muted">{hypothesis.narrative}</p>
      <p className="amount mt-2 text-sm">{formatMoneyDetail(hypothesis.impact)}</p>
      <div className="mt-2">
        <EvidenceChips keys={hypothesis.supporting_evidence} />
      </div>
      <ReviewList reviews={hypothesis.reviews} />
    </article>
  )
}

function OptionCard({ option }: { option: ResolutionOptionResponse }) {
  return (
    <article className="rounded border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-ink-muted">{option.option_type}</span>
        {option.requires_human_approval && (
          <span className="rounded border border-warn-border bg-warn-bg px-1.5 py-0.5 text-xs text-warn-text">
            Needs human approval
          </span>
        )}
      </div>
      <p className="mt-2 text-sm font-medium">{option.title}</p>
      <p className="text-sm text-ink-muted">{option.rationale}</p>
      <div className="mt-2">
        <EvidenceChips keys={option.supporting_evidence} />
      </div>
      {/* Rendered as text, never as a control. Doing anything here needs the approval
          workflow, which does not exist yet. */}
      <p className="mt-2 text-xs text-ink-faint">
        This system does not carry out this step. It records what was suggested.
      </p>
      <ReviewList reviews={option.reviews} />
    </article>
  )
}

function ReviewForm({
  onSubmit,
  busy,
  disabled,
  hint,
}: {
  onSubmit: (
    action: ReviewAction,
    rationale: string,
    amendedNarrative?: string,
  ) => Promise<void>
  busy: boolean
  disabled: boolean
  hint?: string
}) {
  const [action, setAction] = useState<ReviewAction>('ACCEPT')
  const [rationale, setRationale] = useState('')
  const [amended, setAmended] = useState('')

  const needsText = action === 'AMEND'
  // `busy` is part of the guard as well as `disabled`: without it a double-click on
  // Record would post the same review twice, and reviews are append-only.
  const invalid =
    disabled || busy || (needsText ? amended.trim() === '' : rationale.trim() === '')

  return (
    <form
      className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (!invalid) void onSubmit(action, rationale, needsText ? amended : undefined)
      }}
    >
      <label className="sr-only" htmlFor="review-action">
        Review action
      </label>
      <select
        id="review-action"
        value={action}
        onChange={(event) => setAction(event.target.value as ReviewAction)}
        disabled={disabled}
        className="rounded border border-line px-2 py-1.5 text-sm"
      >
        {REVIEW_ACTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>

      {needsText ? (
        <>
          <label className="sr-only" htmlFor="amended-narrative">
            Replacement wording
          </label>
          <input
            id="amended-narrative"
            value={amended}
            onChange={(event) => setAmended(event.target.value)}
            placeholder="Replacement wording"
            disabled={disabled}
            className="min-w-64 flex-1 rounded border border-line px-2 py-1.5 text-sm"
          />
        </>
      ) : (
        <>
          <label className="sr-only" htmlFor="review-rationale">
            Rationale
          </label>
          <input
            id="review-rationale"
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            placeholder="Why?"
            disabled={disabled}
            className="min-w-64 flex-1 rounded border border-line px-2 py-1.5 text-sm"
          />
        </>
      )}

      <button
        type="submit"
        disabled={invalid}
        className="rounded border border-line bg-surface px-3 py-1.5 text-sm font-medium hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
      >
        Record
      </button>
      {hint && <span className="w-full text-xs text-warn-text">{hint}</span>}
    </form>
  )
}

function EvidencePanel({ detail }: { detail: CaseDetail }) {
  return (
    <section>
      <h2 className="text-sm font-semibold">Evidence ({detail.evidence.length})</h2>
      <p className="text-xs text-ink-muted">
        Every key a finding may cite. A citation that names something absent from this
        list is a bug, not a gap.
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-ink-faint">
            <tr>
              <th className="py-1 pr-4 font-medium">Natural key</th>
              <th className="py-1 pr-4 font-medium">Type</th>
              <th className="py-1 font-medium">Content hash</th>
            </tr>
          </thead>
          <tbody>
            {detail.evidence.map((item) => (
              <tr key={`${item.natural_key}-${item.content_hash}`} className="border-t border-line">
                <td className="py-1 pr-4 font-mono">{item.natural_key}</td>
                <td className="py-1 pr-4 text-ink-muted">{item.evidence_type}</td>
                <td className="py-1">
                  <Fingerprint value={item.content_hash} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function HistoryPanel({
  runs,
  currentId,
}: {
  runs: InvestigationResponse[]
  currentId: string | null
}) {
  if (runs.length <= 1) return null
  return (
    <section>
      <h2 className="text-sm font-semibold">Run history</h2>
      <p className="text-xs text-ink-muted">
        Superseded runs are kept. Comparing a run against what the evidence said then is
        the reason they exist.
      </p>
      <ul className="mt-2 space-y-2">
        {runs.map((run) => (
          <li key={run.id} className="rounded border border-line bg-surface px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Version {run.version}</span>
              {run.id === currentId && <span className="text-ink-muted">(shown above)</span>}
              <StaleBadge isStale={run.is_stale} version={run.version} />
              <span className="text-ink-muted">
                read <Fingerprint value={run.evidence_fingerprint} label="run fingerprint" />
              </span>
              <span className="text-ink-faint">{run.created_at}</span>
            </div>
            {run.calculation && (
              <p className="amount mt-1">
                recalculated {run.calculation.recalculated_total}{' '}
                {run.calculation.currency}
                {run.calculation.is_provisional && ' (provisional)'}
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
