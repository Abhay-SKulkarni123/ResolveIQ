/**
 * The queue: what is waiting on a human, and what is waiting on nothing.
 *
 * The filter defaults to "awaiting review" rather than "all". The most common reason to
 * open this screen is to find something to do, and a list of cases that need no
 * decision is a list of things to skip past. Staleness is shown per row because a case
 * whose newest run no longer describes its evidence needs a re-run, not a review --
 * different work, and the reviewer should be able to tell them apart at a glance.
 *
 * A row shows the outstanding figure and the finding count, because those are the two
 * things the backend's list response actually carries for triage. It shows no
 * description: the API omits narratives from the list on purpose, and rendering a
 * blank line under every case would look like a bug rather than a decision.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api } from '../api/client'
import type { CaseList, CaseStatus } from '../api/types'
import { formatMoney } from '../components/money'
import { EmptyState, ErrorPanel, StaleBadge, StatusBadge } from '../components/primitives'

const FILTERS: { value: CaseStatus | undefined; label: string }[] = [
  { value: 'AWAITING_REVIEW', label: 'Awaiting review' },
  { value: 'REOPENED', label: 'Reopened' },
  { value: 'OPEN', label: 'Not yet investigated' },
  { value: undefined, label: 'All' },
]

export function CaseQueue({ onSelect }: { onSelect: (caseId: string) => void }) {
  const [status, setStatus] = useState<CaseStatus | undefined>('AWAITING_REVIEW')
  const [list, setList] = useState<CaseList | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setList(await api.listCases(status === undefined ? { limit: 50 } : { status, limit: 50 }))
      setError(null)
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught : new ApiError(0, 'UNKNOWN', String(caught), null, null),
      )
    } finally {
      setLoading(false)
    }
  }, [status])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">Dispute cases</h1>
        <div className="ml-auto flex gap-1">
          {FILTERS.map((filter) => (
            <button
              key={filter.label}
              type="button"
              onClick={() => setStatus(filter.value)}
              aria-pressed={status === filter.value}
              className={`rounded border px-2 py-1 text-xs ${
                status === filter.value
                  ? 'border-ink bg-ink text-white'
                  : 'border-line bg-surface text-ink-muted hover:bg-gray-50'
              }`}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <ErrorPanel
          title={error.isRetryable ? 'The database is unavailable' : 'Could not load the queue'}
          message={error.message}
          code={error.code}
        />
      )}

      {loading && !list && <EmptyState>Loading...</EmptyState>}

      {list && list.items.length === 0 && !error && (
        <EmptyState>
          Nothing here. A case appears once it has been investigated, or once evidence
          arrives on one that was.
        </EmptyState>
      )}

      {list && list.items.length > 0 && (
        <ul className="divide-y divide-line rounded border border-line bg-surface">
          {list.items.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                onClick={() => onSelect(item.id)}
                className="flex w-full flex-col gap-1 px-4 py-3 text-left hover:bg-canvas"
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-medium">{item.external_id}</span>
                  <StatusBadge status={item.status} />
                  <StaleBadge isStale={item.is_stale} />
                  {item.current_investigation_version !== null && (
                    <span className="text-xs text-ink-faint">
                      run v{item.current_investigation_version}
                    </span>
                  )}
                  {item.finding_count > 0 && (
                    <span className="text-xs text-ink-faint">
                      {item.finding_count} finding{item.finding_count === 1 ? '' : 's'}
                    </span>
                  )}
                </span>
                <span className="text-sm text-ink-muted">
                  outstanding {formatMoney(item.outstanding, item.currency)}
                </span>
                <span className="text-xs text-ink-faint">
                  invoice <span className="font-mono">{item.invoice_external_id}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {list && list.total > list.items.length && (
        <p className="text-xs text-ink-muted">
          Showing {list.items.length} of {list.total}.
        </p>
      )}
    </div>
  )
}
