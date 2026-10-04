/**
 * Small presentational pieces shared across the workbench.
 *
 * Kept together because each one exists to make a specific claim visible, and the claim
 * is the point. A badge that says "stale" is not decoration: it is the UI's half of the
 * guarantee that nobody reads a run without being told its evidence has changed.
 */

import type { AmountText, Capabilities, CaseStatus } from '../api/types'
import { formatDifference, formatMoney, shortFingerprint } from './money'

const STATUS_LABEL: Record<CaseStatus, string> = {
  OPEN: 'Open',
  INVESTIGATING: 'Investigating',
  AWAITING_REVIEW: 'Awaiting review',
  REOPENED: 'Reopened',
  RESOLVED: 'Resolved',
  REJECTED: 'Rejected',
}

export function StatusBadge({ status }: { status: CaseStatus }) {
  const tone =
    status === 'AWAITING_REVIEW'
      ? 'bg-blue-50 text-blue-800 border-blue-200'
      : status === 'REOPENED'
        ? 'bg-warn-bg text-warn-text border-warn-border'
        : status === 'RESOLVED'
          ? 'bg-emerald-50 text-emerald-800 border-emerald-200'
          : status === 'REJECTED'
            ? 'bg-danger-bg text-danger-text border-danger-border'
            : 'bg-gray-50 text-ink-muted border-line'
  return (
    <span className={`inline-flex rounded border px-2 py-0.5 text-xs font-medium ${tone}`}>
      {STATUS_LABEL[status]}
    </span>
  )
}

/**
 * The staleness badge.
 *
 * Rendered whenever a case or a run can be stale, and never suppressed in favour of a
 * subtler treatment: this is the single most consequential thing the UI communicates.
 */
export function StaleBadge({ isStale, version }: { isStale: boolean; version?: number }) {
  if (!isStale) {
    return (
      <span className="inline-flex items-center rounded border border-line bg-surface px-2 py-0.5 text-xs text-ink-muted">
        Current
      </span>
    )
  }
  return (
    <span
      className="inline-flex items-center rounded border border-warn-border bg-warn-bg px-2 py-0.5 text-xs font-medium text-warn-text"
      title="This investigation read evidence that is no longer attached to the case. Its conclusions may no longer hold."
    >
      Stale{version !== undefined ? ` (v${version})` : ''}
    </span>
  )
}

/**
 * The ephemeral-data banner.
 *
 * Shown whenever the store is not durable. A reviewer who cannot see this will treat
 * the queue as the record of a dispute, and a restart will silently take it away.
 */
export function StoreBanner({ capabilities }: { capabilities: Capabilities }) {
  const ephemeral = !capabilities.persistence_is_durable
  const demoIdentity = !capabilities.identity.is_authentication
  if (!ephemeral && !demoIdentity) return null

  return (
    <div className="border-b border-warn-border bg-warn-bg px-4 py-2 text-xs text-warn-text">
      {ephemeral && (
        <p>
          <strong>Development mode.</strong> Cases are held in the{' '}
          <code className="font-mono">{capabilities.store}</code> store, which is not
          durable: they are lost when the server restarts. Nothing here is the record of
          the dispute.
        </p>
      )}
      {demoIdentity && (
        <p className={ephemeral ? 'mt-1' : ''}>
          <strong>Identity is a header, not authentication.</strong> Anyone can send any{' '}
          <code className="font-mono">X-Actor-Id</code>; it is a label on a review, not a
          verified identity, and nothing is authorised by it.
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
  return (
    <span className={`amount ${muted ? 'text-ink-muted' : ''}`}>
      {formatMoney(value, currency)}
    </span>
  )
}

export function Difference({
  value,
  currency,
}: {
  value: AmountText | null
  currency: string | null
}) {
  return <span className="amount font-medium">{formatDifference(value, currency)}</span>
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded border border-dashed border-line bg-surface px-4 py-8 text-center text-sm text-ink-muted">
      {children}
    </div>
  )
}

export function ErrorPanel({ title, message, code }: { title: string; message: string; code?: string }) {
  return (
    <div
      role="alert"
      className="rounded border border-danger-border bg-danger-bg px-4 py-3 text-sm text-danger-text"
    >
      <p className="font-medium">{title}</p>
      <p className="mt-1">{message}</p>
      {code && <p className="mt-1 font-mono text-xs opacity-70">{code}</p>}
    </div>
  )
}
