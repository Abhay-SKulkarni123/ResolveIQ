/**
 * The queue: what is waiting on a human, and what is waiting on nothing.
 *
 * The filter defaults to "awaiting review" rather than "all". The most common reason
 * to open this screen is to find something to do, and a list of cases that need no
 * decision is a list of things to skip past. Staleness is shown per row because a
 * case whose newest run no longer describes its evidence needs a re-run, not a
 * review -- different work, and the reviewer should be able to tell them apart at a
 * glance.
 *
 * A row shows the outstanding figure, the run version and the finding count,
 * because those are the three things the backend's list response actually carries
 * for triage. It shows no description: the API omits narratives from the list on
 * purpose, and rendering a blank line under every case would look like a bug rather
 * than a decision.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api } from '../api/client'
import type { CaseList, CaseStatus } from '../api/types'
import { formatMoney } from '../components/money'
import { EmptyStateBlock, IconButton, Panel, Skeleton, Stat } from '../components/ui'
import { ErrorPanel, StaleBadge, StatusBadge } from '../components/primitives'
import { IconChevronRight, IconInbox, IconRefresh } from '../components/icons'

const FILTERS: { value: CaseStatus | undefined; label: string }[] = [
  { value: 'AWAITING_REVIEW', label: 'Awaiting review' },
  { value: 'REOPENED', label: 'Reopened' },
  { value: 'OPEN', label: 'Not yet investigated' },
  { value: undefined, label: 'All' },
]

interface Summary {
  untriaged: number
  awaiting: number
  reopened: number
  closed: number
}

export function CaseQueue({
  status,
  onSelectStatus,
  onSelect,
}: {
  status: CaseStatus | undefined
  onSelectStatus: (status: CaseStatus | undefined) => void
  onSelect: (caseId: string) => void
}) {
  const [list, setList] = useState<CaseList | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const [summary, setSummary] = useState<Summary | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setList(await api.listCases(status === undefined ? { limit: 50 } : { status, limit: 50 }))
      setError(null)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, 'UNKNOWN', String(caught), null, null))
    } finally {
      setLoading(false)
    }
  }, [status])

  useEffect(() => {
    void load()
  }, [load, reload])

  // One read-only roll-up, fetched once. The list endpoint is the only source of a
  // count, so the four numbers are four small requests rather than a metric the
  // backend does not expose. If they cannot be read, the strip is simply absent:
  // the queue itself still tells the reviewer what to do.
  useEffect(() => {
    let cancelled = false
    async function loadSummary() {
      try {
        const [open, awaiting, reopened, resolved, rejected] = await Promise.all([
          api.listCases({ status: 'OPEN', limit: 1 }),
          api.listCases({ status: 'AWAITING_REVIEW', limit: 1 }),
          api.listCases({ status: 'REOPENED', limit: 1 }),
          api.listCases({ status: 'RESOLVED', limit: 1 }),
          api.listCases({ status: 'REJECTED', limit: 1 }),
        ])
        if (!cancelled) {
          setSummary({
            untriaged: open.total,
            awaiting: awaiting.total,
            reopened: reopened.total,
            closed: resolved.total + rejected.total,
          })
        }
      } catch {
        // A count that cannot be read is not worth an error banner over the queue.
      }
    }
    void loadSummary()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-ink">Dispute cases</h1>
        <p className="max-w-2xl text-sm leading-relaxed text-ink-muted">
          Every dispute the engine has investigated, newest first. Open one to check the figures,
          read the evidence, and record a decision. Nothing here moves money.
        </p>
      </header>

      {summary && <SummaryStrip summary={summary} />}

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by status">
          {FILTERS.map((filter) => {
            const active = status === filter.value
            return (
              <button
                key={filter.label}
                type="button"
                onClick={() => onSelectStatus(filter.value)}
                aria-pressed={active}
                className={
                  'rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ' +
                  (active
                    ? 'border-ink bg-ink text-white'
                    : 'border-line bg-surface text-ink-muted hover:bg-surface-sunken hover:text-ink')
                }
              >
                {filter.label}
              </button>
            )
          })}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {list && (
            <span className="text-xs text-ink-faint tabular-nums">
              {list.total} case{list.total === 1 ? '' : 's'}
            </span>
          )}
          <IconButton label="Refresh queue" onClick={() => setReload((value) => value + 1)}>
            <IconRefresh size={15} />
          </IconButton>
        </div>
      </div>

      {error && (
        <ErrorPanel
          title={error.isRetryable ? 'The database is unavailable' : 'Could not load the queue'}
          message={error.message}
          code={error.code}
        />
      )}

      {!error && (
        <Panel className="overflow-hidden">
          {loading && !list ? (
            <QueueSkeleton />
          ) : list && list.items.length === 0 ? (
            <EmptyStateBlock
              icon={<IconInbox size={18} />}
              title="Nothing in this list"
              description="A case appears once it has been investigated, or once evidence arrives on one that was. Try another filter, or open a new dispute."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[46rem] text-left text-sm">
                <thead>
                  <tr className="text-2xs uppercase tracking-wide text-ink-faint">
                    <th scope="col" className="px-4 py-2.5 font-medium">
                      Case
                    </th>
                    <th scope="col" className="px-4 py-2.5 font-medium">
                      Status
                    </th>
                    <th scope="col" className="px-4 py-2.5 font-medium">
                      Evidence
                    </th>
                    <th scope="col" className="px-4 py-2.5 text-right font-medium">
                      Findings
                    </th>
                    <th scope="col" className="px-4 py-2.5 text-right font-medium">
                      Outstanding
                    </th>
                    <th scope="col" className="px-4 py-2.5 font-medium">
                      Opened
                    </th>
                    <th scope="col" className="w-8 px-4 py-2.5">
                      <span className="sr-only">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {list?.items.map((item) => (
                    <tr
                      key={item.id}
                      onClick={() => onSelect(item.id)}
                      className="cursor-pointer border-t border-line align-middle transition-colors hover:bg-surface-subtle"
                    >
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation()
                            onSelect(item.id)
                          }}
                          className="font-mono text-sm font-medium text-ink underline-offset-2 hover:text-accent hover:underline"
                        >
                          {item.external_id}
                        </button>
                        <span className="mt-0.5 block text-xs text-ink-faint">
                          invoice <span className="font-mono">{item.invoice_external_id}</span>
                        </span>
                        {item.contract_external_id && (
                          <span className="block text-xs text-ink-faint">
                            contract <span className="font-mono">{item.contract_external_id}</span>
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={item.status} />
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <StaleBadge isStale={item.is_stale} />
                          {item.current_investigation_version !== null && (
                            <span className="text-xs text-ink-faint tabular-nums">
                              run v{item.current_investigation_version}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums">
                        {item.finding_count > 0 ? item.finding_count : <span className="text-ink-faint">--</span>}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <span className="amount">{formatMoney(item.outstanding, item.currency)}</span>
                      </td>
                      <td className="px-4 py-3 text-xs text-ink-muted">{shortDate(item.created_at)}</td>
                      <td className="px-4 py-3 text-ink-faint">
                        <IconChevronRight size={15} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}

      {list && list.total > list.items.length && (
        <p className="text-xs text-ink-muted">
          Showing {list.items.length} of {list.total}.
        </p>
      )}
    </div>
  )
}

function SummaryStrip({ summary }: { summary: Summary }) {
  return (
    <Panel as="section" className="px-4 py-3">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Stat label="Awaiting review" value={summary.awaiting} />
        <Stat label="Reopened" value={summary.reopened} />
        <Stat label="Not yet investigated" value={summary.untriaged} />
        <Stat label="Closed" value={summary.closed} />
      </dl>
    </Panel>
  )
}

function QueueSkeleton() {
  return (
    <div className="divide-y divide-line">
      {[0, 1, 2, 3, 4].map((row) => (
        <div key={row} className="flex items-center gap-4 px-4 py-3.5">
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-32" />
            <Skeleton className="h-3 w-48" />
          </div>
          <Skeleton className="h-5 w-24 rounded-full" />
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  )
}

function shortDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}