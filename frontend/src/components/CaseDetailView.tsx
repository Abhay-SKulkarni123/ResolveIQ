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
 *   close       -> the verdict, once a review exists and only for a reviewer
 *   history     -> every run, including superseded ones, and every closed verdict
 *   audit       -> the whole sequence, append-only
 *
 * Three kinds of claim are kept visually distinct, because conflating them is how a
 * reviewer ends up treating a model's guess as a fact: the deterministic engine's
 * figures, the model's findings and hypotheses, and the reviewers' annotations. The
 * review workbench never renders a control that moves money.
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { ApiError, api } from '../api/client'
import type {
  AuditEventResponse,
  AuditHistory,
  CaseDetail,
  DecisionKind,
  DecisionResponse,
  FindingResponse,
  HypothesisResponse,
  InvestigationResponse,
  ResolutionOptionResponse,
  ReviewAction,
  ReviewResponse,
} from '../api/types'
import { cn } from './cn'
import { IconArrowLeft, IconLock } from './icons'
import { formatMoneyDetail } from './money'
import {
  Amount,
  Difference,
  EmptyState,
  ErrorPanel,
  Fingerprint,
  SeverityBadge,
  StaleBadge,
  StatusBadge,
} from './primitives'
import {
  Alert,
  Badge,
  Button,
  Disclosure,
  Field,
  Panel,
  PanelBody,
  PanelHeader,
  Select,
  Skeleton,
  TextInput,
} from './ui'

const REVIEW_ACTIONS: { value: ReviewAction; label: string }[] = [
  { value: 'ACCEPT', label: 'Accept' },
  { value: 'REJECT', label: 'Reject' },
  { value: 'REQUEST_MORE_INFO', label: 'Request more info' },
  { value: 'AMEND', label: 'Amend wording' },
]

export function CaseDetailView({
  caseId,
  actorRole,
  onBack,
  onChanged,
}: {
  caseId: string
  actorRole: string
  onBack: () => void
  onChanged?: () => void
}) {
  const [detail, setDetail] = useState<CaseDetail | null>(null)
  const [audit, setAudit] = useState<AuditHistory | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      // The trail is a separate read from the detail, so it is fetched alongside it
      // rather than embedded in it: a case with a long history does not weigh down the
      // response every viewer loads, and a failed audit read does not stop the case
      // itself from rendering.
      const [nextDetail, nextAudit] = await Promise.all([
        api.getCase(caseId),
        api.getAudit(caseId, { limit: 100 }),
      ])
      setDetail(nextDetail)
      setAudit(nextAudit)
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
    return (
      <div className="space-y-4">
        <Breadcrumb externalId={null} onBack={onBack} />
        <ErrorPanel title="Could not load this case" message={error.message} code={error.code} />
      </div>
    )
  }
  if (!detail) {
    return <DetailSkeleton />
  }

  // The backend names the live run; it does not make the client re-derive it. The old
  // lookup compared a run id against the *case* id, which can never be equal, so it
  // always fell through to "last run wins" and silently mislabelled reopened cases.
  const current = detail.current_investigation
  const latest = current ?? detail.investigations[detail.investigations.length - 1]
  const reviewedOnLatest = Boolean(
    latest && detail.reviews.some((review) => review.investigation_id === latest.id),
  )
  const isReviewer = actorRole === 'reviewer'
  const canClose = detail.status === 'AWAITING_REVIEW' && isReviewer

  return (
    <div className="space-y-6">
      <Breadcrumb externalId={detail.external_id} onBack={onBack} />

      <CaseHeader detail={detail} />

      <NextStep detail={detail} hasRun={latest !== undefined} reviewed={reviewedOnLatest} isReviewer={isReviewer} />

      {notice && (
        <Alert tone="success" live>
          {notice}
        </Alert>
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

      <section className="flex flex-wrap items-center gap-2">
        <Button
          variant={latest === undefined || detail.is_stale ? 'primary' : 'secondary'}
          loading={busy}
          onClick={() => run(() => api.investigate(detail.id), 'Investigation recorded.')}
        >
          Run investigation
        </Button>
        <ReopenControl
          busy={busy}
          closed={detail.status === 'RESOLVED' || detail.status === 'REJECTED'}
          onReopen={(reason) =>
            run(() => api.reopen(detail.id, reason), 'Case reopened. It needs a new run.')
          }
        />
        {/* No approve, no adjust, no execute. Those arrive with the later guards. */}
      </section>

      {detail.is_stale && latest && (
        <Alert tone="warning" title="The newest run no longer describes this case.">
          Evidence was attached after version {latest.version} ran, so its findings and figures
          describe records that are no longer on the dispute. Re-run before reviewing.
        </Alert>
      )}

      {latest ? (
        <RunView caseId={detail.id} run={latest} onReview={run} busy={busy} />
      ) : (
        <EmptyState>
          <span className="font-medium text-ink">Not investigated yet.</span> Run an investigation
          to price the invoice and surface findings.
        </EmptyState>
      )}

      {canClose && latest && (
        <DecidePanel
          busy={busy}
          reviewed={reviewedOnLatest}
          onDecide={(decision, rationale) =>
            run(
              () => api.decide(detail.id, decision, rationale),
              decision === 'RESOLVED' ? 'Case resolved.' : 'Case rejected.',
            )
          }
        />
      )}

      <EvidencePanel detail={detail} />
      <HistoryPanel runs={detail.investigations} currentId={latest?.id ?? null} />
      {detail.decisions.length > 0 && <DecisionHistory decisions={detail.decisions} />}
      {audit && audit.items.length > 0 && <AuditPanel history={audit} />}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Header and framing                                                         */
/* -------------------------------------------------------------------------- */

function Breadcrumb({ externalId, onBack }: { externalId: string | null; onBack: () => void }) {
  return (
    <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-xs">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1 rounded-md text-ink-muted transition-colors hover:text-ink"
      >
        <IconArrowLeft size={14} />
        Disputes
      </button>
      {externalId && (
        <>
          <span className="text-ink-faint" aria-hidden="true">
            /
          </span>
          <span className="font-mono text-ink-muted">{externalId}</span>
        </>
      )}
    </nav>
  )
}

function CaseHeader({ detail }: { detail: CaseDetail }) {
  return (
    <header className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="font-mono text-xl font-semibold tracking-tight text-ink">{detail.external_id}</h1>
        <StatusBadge status={detail.status} />
        <StaleBadge isStale={detail.is_stale} />
        <SeverityBadge severity={detail.severity} />
      </div>
      <p className="max-w-3xl text-sm leading-relaxed text-ink-muted">{detail.description}</p>
      <dl className="flex flex-wrap gap-x-6 gap-y-1.5 text-xs text-ink-muted">
        <Meta label="Invoice" value={<span className="font-mono">{detail.invoice_external_id}</span>} />
        {detail.contract_external_id && (
          <Meta label="Contract" value={<span className="font-mono">{detail.contract_external_id}</span>} />
        )}
        <Meta label="Balance" value={<Amount value={detail.outstanding} currency={detail.currency} />} />
        <Meta label="Findings" value={<span className="tabular-nums">{detail.finding_count}</span>} />
        <Meta label="Version" value={<span className="tabular-nums">{detail.version}</span>} />
        <Meta
          label="Evidence"
          value={<Fingerprint value={detail.evidence_fingerprint} label="evidence fingerprint" />}
        />
      </dl>
    </header>
  )
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-center gap-1.5">
      <dt className="text-ink-faint">{label}</dt>
      <dd className="text-ink">{value}</dd>
    </div>
  )
}

/**
 * The one sentence that says where the case is and what happens next.
 *
 * It is derived only from state the server sends -- status, staleness, whether the
 * newest run has been reviewed, and the caller's role -- so it can never advise an
 * action the backend would refuse.
 */
function NextStep({
  detail,
  hasRun,
  reviewed,
  isReviewer,
}: {
  detail: CaseDetail
  hasRun: boolean
  reviewed: boolean
  isReviewer: boolean
}) {
  const step = describeNextStep(detail.status, detail.is_stale, hasRun, reviewed, isReviewer)
  if (!step) return null
  const tone = detail.is_stale ? 'warning' : step.tone
  return (
    <Alert tone={tone} title={step.title}>
      {step.body}
    </Alert>
  )
}

function describeNextStep(
  status: CaseDetail['status'],
  isStale: boolean,
  hasRun: boolean,
  reviewed: boolean,
  isReviewer: boolean,
): { title: string; body: string; tone: 'info' | 'warning' | 'success' } | null {
  if (isStale) {
    return {
      title: 'Re-run before reviewing',
      body: 'New evidence was attached after the newest run, so its figures describe records that have changed. Start a fresh run to bring the case up to date.',
      tone: 'warning',
    }
  }
  switch (status) {
    case 'OPEN':
    case 'INVESTIGATING':
      return hasRun
        ? {
            title: 'Review the findings',
            body: 'A run is recorded. Check each finding against the evidence it cites, then close the case.',
            tone: 'info',
          }
        : {
            title: 'Run the investigation',
            body: 'Nothing has been priced yet. A run applies the contract terms to the usage and reports what it finds.',
            tone: 'info',
          }
    case 'REOPENED':
      return {
        title: 'This case was reopened',
        body: 'Its earlier verdict still stands in the history, but the case needs a fresh run before it can be reviewed again.',
        tone: 'info',
      }
    case 'AWAITING_REVIEW':
      if (!isReviewer) {
        return {
          title: 'Awaiting a reviewer',
          body: 'This case is ready to close. Recording the verdict is a reviewer action; you are viewing as an analyst.',
          tone: 'info',
        }
      }
      return reviewed
        ? {
            title: 'Ready to close',
            body: 'The newest run has at least one recorded review. Record the verdict that closes the case when you are satisfied.',
            tone: 'success',
          }
        : {
            title: 'Review before closing',
            body: 'Read each finding against its citations and record a decision on it. The verdict unlocks once the run has been reviewed.',
            tone: 'info',
          }
    case 'RESOLVED':
    case 'REJECTED':
      return {
        title: 'Case closed',
        body: 'Its verdict is recorded below. If the picture changes, reopen the case with a reason and it will need a new run.',
        tone: 'success',
      }
    default:
      return null
  }
}

/* -------------------------------------------------------------------------- */
/* Actions                                                                    */
/* -------------------------------------------------------------------------- */

function ReopenControl({
  onReopen,
  busy,
  closed,
}: {
  onReopen: (reason: string) => void
  busy: boolean
  closed: boolean
}) {
  const [reason, setReason] = useState('')
  if (!closed) {
    return (
      <span className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-ink-faint">
        Reopen is available once the case is closed
      </span>
    )
  }
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Field label="Reason for reopening" className="w-72">
        <TextInput
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="What changed?"
        />
      </Field>
      <Button disabled={reason.trim() === '' || busy} onClick={() => onReopen(reason)}>
        Reopen
      </Button>
    </div>
  )
}

function DecidePanel({
  busy,
  reviewed,
  onDecide,
}: {
  busy: boolean
  reviewed: boolean
  onDecide: (decision: DecisionKind, rationale: string) => void
}) {
  const [rationale, setRationale] = useState('')
  const rationaleMissing = rationale.trim() === ''
  return (
    <Panel>
      <PanelHeader
        title="Close the case"
        description="The verdict is a lifecycle change. It moves no money."
      />
      <PanelBody className="space-y-3">
        {!reviewed && (
          <Alert tone="warning">
            Nothing has been reviewed on the newest run yet. The backend will refuse a verdict until
            it has.
          </Alert>
        )}
        <Field
          label="Decision rationale"
          hint="Why this verdict is the right one. The verdict carries no other record of the reasoning."
        >
          <TextInput
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            placeholder="e.g. the corrected usage matches the export"
            disabled={busy || !reviewed}
          />
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            disabled={busy || !reviewed || rationaleMissing}
            loading={busy}
            onClick={() => onDecide('RESOLVED', rationale)}
          >
            Resolve
          </Button>
          <Button
            variant="danger"
            disabled={busy || !reviewed || rationaleMissing}
            onClick={() => onDecide('REJECTED', rationale)}
          >
            Reject
          </Button>
        </div>
      </PanelBody>
    </Panel>
  )
}

/* -------------------------------------------------------------------------- */
/* A run                                                                      */
/* -------------------------------------------------------------------------- */

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
    <div className="space-y-6">
      <Panel>
        <PanelHeader title={`Run v${run.version}`} description={`Recorded ${run.created_at}`} actions={<StaleBadge isStale={run.is_stale} />} />
        <PanelBody>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-3 lg:grid-cols-6">
            <Meta label="Provider" value={<span className="font-mono">{run.provider_name}</span>} />
            <Meta label="Model" value={<span className="font-mono">{run.model}</span>} />
            <Meta label="Prompt" value={<span className="font-mono">{run.prompt_version}</span>} />
            <Meta label="Engine" value={<span className="font-mono">{run.engine_version}</span>} />
            <Meta label="Status" value={<span className="font-mono">{run.status}</span>} />
            <Meta
              label="Read"
              value={<Fingerprint value={run.evidence_fingerprint} label="run fingerprint" />}
            />
          </dl>
          <p className="mt-3 text-xs text-ink-faint">
            The model reports findings; the engine computes the figures. They are not the same claim.
          </p>
        </PanelBody>
      </Panel>

      {run.degradations.length > 0 && (
        <Alert tone="warning" title="This run is degraded.">
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {run.degradations.map((degradation) => (
              <li key={degradation}>{degradation}</li>
            ))}
          </ul>
          <p className="mt-1">
            Treat its conclusions as incomplete. Nothing here should be relied on without checking.
          </p>
        </Alert>
      )}

      {run.calculation && <CalculationPanel calculation={run.calculation} />}

      <Panel>
        <PanelHeader title="What the run concluded" />
        <PanelBody>
          <p className="text-sm leading-relaxed text-ink">{run.summary}</p>
        </PanelBody>
      </Panel>

      {run.findings.length > 0 && (
        <section className="space-y-3">
          <SectionHeading
            title="Findings"
            description="Each finding is the model's claim, supported by the evidence keys it cites. A reviewer decides whether it holds."
          />
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
        </section>
      )}

      {run.hypotheses.length > 0 && (
        <section className="space-y-3">
          <SectionHeading
            title="Hypotheses"
            description="Unconfirmed explanations the model offered. These are not established facts."
          />
          {run.hypotheses.map((item) => (
            <HypothesisCard key={item.id} hypothesis={item} />
          ))}
        </section>
      )}

      {run.resolution_options.length > 0 && (
        <section className="space-y-3">
          <SectionHeading
            title="Suggested next steps"
            description="What the system suggests doing. It records the suggestion and carries nothing out."
          />
          {run.resolution_options.map((item) => (
            <OptionCard key={item.id} option={item} />
          ))}
        </section>
      )}
    </div>
  )
}

function SectionHeading({ title, description }: { title: string; description: string }) {
  return (
    <div>
      <h2 className="text-sm font-semibold text-ink">{title}</h2>
      <p className="mt-0.5 max-w-2xl text-xs text-ink-muted">{description}</p>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Calculation                                                                */
/* -------------------------------------------------------------------------- */

function CalculationPanel({
  calculation,
}: {
  calculation: NonNullable<InvestigationResponse['calculation']>
}) {
  return (
    <Panel className="border-l-2 border-l-accent">
      <PanelHeader
        title="Recalculation"
        description="Computed by the deterministic engine from the stored records, not by the model."
        actions={calculation.is_provisional ? <Badge tone="warning">Provisional</Badge> : null}
      />
      <PanelBody className="space-y-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Figure
            label="Invoiced"
            value={<Amount value={calculation.recorded_total} currency={calculation.currency} />}
          />
          <Figure
            label="Recalculated"
            value={<Amount value={calculation.recalculated_total} currency={calculation.currency} />}
          />
          <Figure
            label="Difference"
            value={<Difference value={calculation.difference ?? null} currency={calculation.currency} />}
          />
          <Figure
            label="Outstanding"
            value={<Amount value={calculation.outstanding ?? null} currency={calculation.currency} />}
          />
        </dl>

        {calculation.unresolved_metrics.length > 0 && (
          <Alert tone="warning">
            These metrics could not be priced, so the recalculated figure is a lower bound:{' '}
            <span className="font-mono">{calculation.unresolved_metrics.join(', ')}</span>.
          </Alert>
        )}

        <Disclosure summary="How these figures were reached">
          <div className="space-y-3 rounded-lg border border-line bg-surface-subtle p-3">
            <div>
              <p className="text-xs font-medium text-ink">Engine</p>
              <p className="font-mono text-xs text-ink-muted">{calculation.engine_version}</p>
            </div>
            <StructureView label="Calculation trace" value={calculation.trace} />
          </div>
        </Disclosure>
      </PanelBody>
    </Panel>
  )
}

function Figure({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface-subtle px-3 py-2.5">
      <dt className="text-2xs font-medium uppercase tracking-wide text-ink-faint">{label}</dt>
      <dd className="mt-1 text-base font-semibold">{value}</dd>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Findings, hypotheses, options                                              */
/* -------------------------------------------------------------------------- */

function EvidenceChips({ keys, tone = 'neutral' }: { keys: string[]; tone?: 'neutral' | 'danger' }) {
  if (keys.length === 0) {
    return <span className="text-xs text-ink-faint">No citations</span>
  }
  return (
    <ul className="flex flex-wrap gap-1">
      {keys.map((key) => (
        <li
          key={key}
          className={cn(
            'rounded border px-1.5 py-0.5 font-mono text-2xs',
            tone === 'danger'
              ? 'border-danger-border bg-danger-bg text-danger-text'
              : 'border-line bg-surface-sunken text-ink-muted',
          )}
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
    <ul className="mt-3 space-y-2 border-l-2 border-accent/30 pl-3">
      {reviews.map((review) => (
        <li key={review.id} className="text-xs">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <Badge tone={review.action === 'REJECT' ? 'danger' : 'accent'} dot={false}>
              {review.action.replace('_', ' ').toLowerCase()}
            </Badge>
            <span className="text-ink-muted">
              {review.actor_id} ({review.actor_role})
            </span>
            {!review.is_against_current_evidence && (
              <span className="text-warning-text">against superseded evidence</span>
            )}
            <Fingerprint value={review.evidence_fingerprint_seen} label="reviewer saw" />
          </div>
          {review.rationale && <p className="mt-1 text-ink-muted">{review.rationale}</p>}
          {review.amended_narrative && (
            <p className="mt-1 text-ink-muted">
              Amended to: <span className="text-ink">{review.amended_narrative}</span>
            </p>
          )}
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
    <article className="rounded-lg border border-line bg-surface shadow-xs">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <span className="font-mono text-xs font-medium text-ink">{finding.code}</span>
        <SeverityBadge severity={finding.severity} />
        <span className="text-xs text-ink-faint">{finding.category}</span>
        <span className="amount ml-auto text-xs text-ink-muted">
          confidence {finding.confidence}
        </span>
      </div>
      <div className="space-y-3 px-4 py-3.5">
        <p className="text-sm leading-relaxed text-ink">{finding.narrative}</p>
        <div>
          <p className="mb-1 text-2xs font-medium uppercase tracking-wide text-ink-faint">
            Supported by
          </p>
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
      </div>
    </article>
  )
}

function HypothesisCard({ hypothesis }: { hypothesis: HypothesisResponse }) {
  return (
    <article className="rounded-lg border border-line bg-surface shadow-xs">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <span className="font-mono text-xs font-medium text-ink">{hypothesis.hypothesis_code}</span>
        <Badge tone="info" dot={false}>
          {hypothesis.status}
        </Badge>
        <Badge tone="neutral" dot={false}>
          not established
        </Badge>
        {hypothesis.likelihood !== null && (
          <span className="amount ml-auto text-xs text-ink-muted">likelihood {hypothesis.likelihood}</span>
        )}
      </div>
      <div className="space-y-3 px-4 py-3.5">
        <p className="text-sm font-medium text-ink">{hypothesis.title}</p>
        <p className="text-sm leading-relaxed text-ink-muted">{hypothesis.narrative}</p>
        <div className="rounded-lg border border-line bg-surface-subtle px-3 py-2">
          <p className="text-2xs font-medium uppercase tracking-wide text-ink-faint">Impact</p>
          <p className="amount mt-0.5 text-sm text-ink">{formatMoneyDetail(hypothesis.impact)}</p>
          {hypothesis.impact_basis && (
            <p className="mt-0.5 text-xs text-ink-muted">{hypothesis.impact_basis}</p>
          )}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <p className="mb-1 text-2xs font-medium uppercase tracking-wide text-ink-faint">
              Supporting
            </p>
            <EvidenceChips keys={hypothesis.supporting_evidence} />
          </div>
          <div>
            <p className="mb-1 text-2xs font-medium uppercase tracking-wide text-ink-faint">
              Refuting
            </p>
            <EvidenceChips keys={hypothesis.refuting_evidence} tone="danger" />
          </div>
        </div>
        <ReviewList reviews={hypothesis.reviews} />
      </div>
    </article>
  )
}

function OptionCard({ option }: { option: ResolutionOptionResponse }) {
  return (
    <article className="rounded-lg border border-line bg-surface shadow-xs">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <span className="font-mono text-xs font-medium text-ink">{option.option_type}</span>
        {option.requires_human_approval && (
          <Badge tone="warning">
            <span className="inline-flex items-center gap-1">
              <IconLock size={12} />
              Needs human approval
            </span>
          </Badge>
        )}
        {option.hypothesis_code && (
          <span className="text-xs text-ink-faint">for {option.hypothesis_code}</span>
        )}
      </div>
      <div className="space-y-3 px-4 py-3.5">
        <p className="text-sm font-medium text-ink">{option.title}</p>
        <p className="text-sm leading-relaxed text-ink-muted">{option.rationale}</p>
        <EvidenceChips keys={option.supporting_evidence} />
        {/* Rendered as text, never as a control. Doing anything here needs the approval
            workflow, which does not exist yet. */}
        <p className="text-xs text-ink-faint">
          This system does not carry out this step. It records what was suggested.
        </p>
        <ReviewList reviews={option.reviews} />
      </div>
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
  const invalid = disabled || busy || (needsText ? amended.trim() === '' : rationale.trim() === '')

  return (
    <form
      className="flex flex-wrap items-start gap-2 border-t border-line pt-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (!invalid) void onSubmit(action, rationale, needsText ? amended : undefined)
      }}
    >
      <Field label="Review action" className="w-44">
        <Select
          value={action}
          onChange={(event) => setAction(event.target.value as ReviewAction)}
          disabled={disabled}
        >
          {REVIEW_ACTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </Field>

      {needsText ? (
        <Field label="Replacement wording" className="min-w-56 flex-1">
          <TextInput
            value={amended}
            onChange={(event) => setAmended(event.target.value)}
            placeholder="The wording to record instead"
            disabled={disabled}
          />
        </Field>
      ) : (
        <Field label="Rationale" className="min-w-56 flex-1">
          <TextInput
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            placeholder="Why?"
            disabled={disabled}
          />
        </Field>
      )}

      <div className="pt-[1.375rem]">
        <Button type="submit" disabled={invalid}>
          Record
        </Button>
      </div>
      {hint && <p className="w-full text-xs text-warning-text">{hint}</p>}
    </form>
  )
}

/* -------------------------------------------------------------------------- */
/* Evidence                                                                   */
/* -------------------------------------------------------------------------- */

function EvidencePanel({ detail }: { detail: CaseDetail }) {
  return (
    <Panel>
      <PanelHeader
        title={`Evidence (${detail.evidence.length})`}
        description="Every key a finding may cite. A citation that names something absent from this list is a bug, not a gap."
      />
      <PanelBody className="space-y-4">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-ink-faint">
              <tr className="text-2xs uppercase tracking-wide">
                <th scope="col" className="py-1.5 pr-4 font-medium">
                  Natural key
                </th>
                <th scope="col" className="py-1.5 pr-4 font-medium">
                  Type
                </th>
                <th scope="col" className="py-1.5 font-medium">
                  Content hash
                </th>
              </tr>
            </thead>
            <tbody>
              {detail.evidence.map((item) => (
                <tr key={`${item.natural_key}-${item.content_hash}`} className="border-t border-line">
                  <td className="py-1.5 pr-4 font-mono text-ink">{item.natural_key}</td>
                  <td className="py-1.5 pr-4 text-ink-muted">{item.evidence_type}</td>
                  <td className="py-1.5">
                    <Fingerprint value={item.content_hash} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {detail.evidence.length > 0 && (
          <Disclosure summary={`Inspect a snapshot (${detail.evidence.length})`}>
            <ul className="space-y-1">
              {detail.evidence.map((item) => (
                <li key={`${item.natural_key}-snapshot`}>
                  <Disclosure summary={item.natural_key} tone="plain">
                    <StructureView label={item.evidence_type} value={item.snapshot} />
                  </Disclosure>
                </li>
              ))}
            </ul>
          </Disclosure>
        )}
      </PanelBody>
    </Panel>
  )
}

/**
 * A readable view of an arbitrary stored object.
 *
 * Snapshots and traces are free-form bags the server passes through, so this shows
 * the top level as labelled rows and drops anything nested into a formatted block.
 * That keeps a small snapshot legible without dumping a wall of JSON for a big one.
 */
function StructureView({ label, value }: { label: string; value: Record<string, unknown> }) {
  const entries = Object.entries(value)
  if (entries.length === 0) {
    return <p className="text-xs text-ink-muted">{label}: nothing recorded.</p>
  }
  return (
    <dl className="space-y-1.5">
      {entries.map(([key, entry]) => (
        <div key={key} className="grid grid-cols-[minmax(0,11rem)_1fr] gap-2">
          <dt className="truncate font-mono text-xs text-ink-muted" title={key}>
            {key}
          </dt>
          <dd className="min-w-0 text-xs text-ink">
            <ValueView value={entry} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

function ValueView({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="text-ink-faint">null</span>
  }
  if (typeof value === 'string') {
    return <span className="break-words">{value}</span>
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return <span className="font-mono">{String(value)}</span>
  }
  return (
    <pre className="mt-0.5 overflow-x-auto rounded bg-surface-sunken p-2 font-mono text-2xs leading-relaxed text-ink-muted">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

/* -------------------------------------------------------------------------- */
/* History, decisions, audit                                                  */
/* -------------------------------------------------------------------------- */

function HistoryPanel({
  runs,
  currentId,
}: {
  runs: InvestigationResponse[]
  currentId: string | null
}) {
  if (runs.length <= 1) return null
  return (
    <Panel>
      <PanelHeader
        title="Run history"
        description="Superseded runs are kept. Comparing a run against what the evidence said then is the reason they exist."
      />
      <PanelBody>
        <ul className="space-y-2">
          {runs.map((run) => (
            <li key={run.id} className="rounded-lg border border-line bg-surface-subtle px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">Version {run.version}</span>
                {run.id === currentId && (
                  <Badge tone="accent" dot={false}>
                    shown above
                  </Badge>
                )}
                <StaleBadge isStale={run.is_stale} version={run.version} />
                <span className="text-ink-muted">
                  read <Fingerprint value={run.evidence_fingerprint} label="run fingerprint" />
                </span>
                <span className="ml-auto text-ink-faint">{run.created_at}</span>
              </div>
              {run.calculation && (
                <p className="amount mt-1 text-ink-muted">
                  recalculated {run.calculation.recalculated_total} {run.calculation.currency}
                  {run.calculation.is_provisional && ' (provisional)'}
                </p>
              )}
            </li>
          ))}
        </ul>
      </PanelBody>
    </Panel>
  )
}

const AUDIT_LABELS: Record<AuditEventResponse['event_type'], string> = {
  CASE_CREATED: 'Case opened',
  EVIDENCE_ATTACHED: 'Evidence attached',
  INVESTIGATION_COMPLETED: 'Investigation completed',
  REVIEW_RECORDED: 'Review recorded',
  DECISION_RECORDED: 'Decision recorded',
  CASE_REOPENED: 'Case reopened',
}

/**
 * A short, human sentence for the one payload field worth surfacing per event kind.
 *
 * The payload is a bag the server passes through untouched, so this reads the few keys
 * it knows and shows nothing for the rest, rather than stringifying the whole object.
 * A reader needs "why was it reopened", not a JSON dump.
 */
function auditDetail(event: AuditEventResponse): string | null {
  const payload = event.payload
  const text = (key: string): string | null =>
    typeof payload[key] === 'string' ? (payload[key] as string) : null
  switch (event.event_type) {
    case 'CASE_REOPENED':
      return text('reason')
    case 'DECISION_RECORDED':
      return text('decision')
    case 'REVIEW_RECORDED':
      return text('action')
    case 'EVIDENCE_ATTACHED':
      return typeof payload['evidence_count'] === 'number'
        ? `${payload['evidence_count']} items`
        : null
    default:
      return null
  }
}

/**
 * The case's audit trail, oldest-first.
 *
 * This is the one place the whole sequence is legible in order: every event, who did
 * it, and the version it left the case at. The panel is read-only by construction --
 * there is no verb here because the backend has none, and an editable audit trail is
 * worth nothing.
 */
function AuditPanel({ history }: { history: AuditHistory }) {
  return (
    <Panel>
      <PanelHeader
        title={`Audit trail (${history.total})`}
        description="Everything that happened to this case, oldest first. Append-only: nothing here can be edited or removed."
      />
      <PanelBody>
        <ol className="space-y-0.5">
          {history.items.map((event) => (
            <li
              key={event.id}
              className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-l-2 border-line py-1 pl-3 text-xs"
            >
              <span className="font-medium text-ink">{AUDIT_LABELS[event.event_type]}</span>
              {auditDetail(event) && <span className="text-ink-muted">{auditDetail(event)}</span>}
              <span className="text-ink-muted">
                {event.actor_id
                  ? event.actor_role
                    ? `by ${event.actor_id} (${event.actor_role})`
                    : `by ${event.actor_id}`
                  : 'by system'}
              </span>
              {event.entity_version !== null && (
                <span className="text-ink-faint">version {event.entity_version}</span>
              )}
              <span className="ml-auto text-ink-faint">{event.occurred_at}</span>
            </li>
          ))}
        </ol>
      </PanelBody>
    </Panel>
  )
}

function DecisionHistory({ decisions }: { decisions: DecisionResponse[] }) {
  return (
    <Panel>
      <PanelHeader
        title="Closing decisions"
        description="Every verdict that closed the case. A reopened case keeps its history."
      />
      <PanelBody>
        <ul className="space-y-2">
          {decisions.map((decision) => (
            <li key={decision.id} className="rounded-lg border border-line bg-surface-subtle px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={decision.decision === 'RESOLVED' ? 'success' : 'danger'}>
                  {decision.decision.toLowerCase()}
                </Badge>
                <span className="text-ink-muted">
                  {decision.actor_id} ({decision.actor_role})
                </span>
                <span className="ml-auto text-ink-faint">{decision.created_at}</span>
              </div>
              {decision.rationale && <p className="mt-1 text-ink-muted">{decision.rationale}</p>}
              <Fingerprint value={decision.evidence_fingerprint_seen} label="decided against" />
            </li>
          ))}
        </ul>
      </PanelBody>
    </Panel>
  )
}

/* -------------------------------------------------------------------------- */
/* Loading                                                                    */
/* -------------------------------------------------------------------------- */

function DetailSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-3 w-32" />
      <div className="space-y-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-4 w-96" />
        <Skeleton className="h-4 w-72" />
      </div>
      <Skeleton className="h-40 w-full rounded-lg" />
      <Skeleton className="h-28 w-full rounded-lg" />
    </div>
  )
}