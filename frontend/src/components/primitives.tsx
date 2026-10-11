/**
 * Small presentational pieces shared across the workbench.
 *
 * Kept together because each one exists to make a specific claim visible, and the
 * claim is the point. A badge that says "stale" is not decoration: it is the UI's
 * half of the guarantee that nobody reads a run without being told its evidence
 * has changed. A status badge is never colour alone -- the tone is emphasised by a
 * word, because a reviewer who cannot see the colour still gets the meaning.
 */

import type { ReactNode } from 'react'

import type { AmountText, Capabilities, CaseStatus } from '../api/types'
import { formatDifference, formatMoney, shortFingerprint } from './money'
import { Badge, type BadgeTone } from './ui'

const STATUS_LABEL: Record<CaseStatus, string> = {
  OPEN: 'Open',
  INVESTIGATING: 'Investigating',
  AWAITING_REVIEW: 'Awaiting review',
  REOPENED: 'Reopened',
  RESOLVED: 'Resolved',
  REJECTED: 'Rejected',
}

/** The tone a status deserves, always paired with its word label. */
const STATUS_TONE: Record<CaseStatus, BadgeTone> = {
  OPEN: 'neutral',
  INVESTIGATING: 'info',
  AWAITING_REVIEW: 'warning',
  REOPENED: 'warning',
  RESOLVED: 'success',
  REJECTED: 'danger',
}

export function StatusBadge({ status }: { status: CaseStatus }) {
  return (
    <Badge tone={STATUS_TONE[status]}>
      {STATUS_LABEL[status]}
    </Badge>
  )
}

export function SeverityBadge({ severity, className }: { severity: string; className?: string }) {
  const tone: BadgeTone =
    severity === 'CRITICAL' || severity === 'HIGH'
      ? 'danger'
      : severity === 'MEDIUM'
        ? 'warning'
        : 'neutral'
  return (
    <Badge tone={tone} dot={false} className={className}>
      {severity}
    </Badge>
  )
}

/**
 * The staleness badge.
 *
 * Rendered whenever a case or a run can be stale, and never suppressed in favour of
 * a subtler treatment: this is the single most consequential thing the UI
 * communicates. "Current" is said out loud too, rather than implied by the absence
 * of a warning.
 */
export function StaleBadge({ isStale, version }: { isStale: boolean; version?: number }) {
  if (!isStale) {
    return (
      <Badge tone="neutral">
        Current
      </Badge>
    )
  }
  return (
    <Badge
      tone="warning"
      className="cursor-help"
      title="This investigation read evidence that is no longer attached to the case. Its conclusions may no longer hold."
    >
      Stale{version !== undefined ? ` (v${version})` : ''}
    </Badge>
  )
}

/**
 * The ephemeral-data banner.
 *
 * Shown whenever the store is not durable or the identity is not authentication. A
 * reviewer who cannot see this will treat the queue as the record of a dispute, and
 * a restart will silently take it away.
 */
export function StoreBanner({ capabilities }: { capabilities: Capabilities }) {
  const ephemeral = !capabilities.persistence_is_durable
  const demoIdentity = !capabilities.identity.is_authentication
  if (!ephemeral && !demoIdentity) return null

  return (
    <div className="space-y-1 rounded-lg border border-warning-border bg-warning-bg px-3.5 py-2.5 text-xs text-warning-text">
      {ephemeral && (
        <p>
          <strong>Development mode.</strong> Cases are held in the{' '}
          <code className="font-mono text-[0.6875rem]">{capabilities.store}</code> store, which is
          not durable: they are lost when the server restarts. Nothing here is the record of
          the dispute.
        </p>
      )}
      {demoIdentity && (
        <p className={ephemeral ? 'mt-1' : ''}>
          <strong>Identity is a header, not authentication.</strong> Anyone can send any{' '}
          <code className="font-mono text-[0.6875rem]">X-Actor-Id</code>; it is a label on a
          review, not a verified identity, and nothing is authorised by it.
        </p>
      )}
    </div>
  )
}

export function Fingerprint({ value, label }: { value: string; label?: string }) {
  return (
    <span
      className="font-mono text-xs text-ink-faint"
      title={value}
      aria-label={label ? `${label} ${value}` : value}
    >
      {shortFingerprint(value)}
    </span>
  )
}

/** A single monetary figure. The class keeps digits aligned down a column. */
export function Amount({
  value,
  currency,
  muted = false,
}: {
  value: AmountText | null
  currency: string | null
  muted?: boolean
}) {
  return <span className={`amount ${muted ? 'text-ink-muted' : 'text-ink'}`}>{formatMoney(value, currency)}</span>
}

export function Difference({
  value,
  currency,
}: {
  value: AmountText | null
  currency: string | null
}) {
  return <span className="amount font-semibold text-accent-text">{formatDifference(value, currency)}</span>
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line-strong bg-surface-subtle px-4 py-10 text-center text-sm text-ink-muted">
      {children}
    </div>
  )
}

export function ErrorPanel({ title, message, code }: { title: string; message: string; code?: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-danger-border bg-danger-bg px-4 py-3 text-sm text-danger-text"
    >
      <p className="font-semibold">{title}</p>
      <p className="mt-0.5 leading-relaxed">{message}</p>
      {code && <p className="mt-1 font-mono text-xs opacity-70">{code}</p>}
    </div>
  )
}