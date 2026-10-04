/**
 * The one place that talks to the backend.
 *
 * Every failure is normalised into an `ApiError` carrying the server's stable `code`,
 * so callers branch on a code instead of parsing prose. The alternative -- checking
 * `response.status` at each call site -- spreads the meaning of each status across the
 * whole UI, and the one place it matters most (a stale review) needs both the status
 * *and* the code to tell "this investigation is stale" from "this case is missing".
 */

import type {
  ApiErrorBody,
  Capabilities,
  CaseDetail,
  CaseList,
  CaseStatus,
  ReviewAction,
  ReviewResponse,
  ReviewTargetType,
} from './types'

const BASE = '/api/v1'

/** Codes the UI reacts to by name. Anything else falls back to the message. */
export const ErrorCode = {
  DisputeNotFound: 'DISPUTE_NOT_FOUND',
  VersionConflict: 'VERSION_CONFLICT',
  InvalidTransition: 'INVALID_TRANSITION',
  NotInvestigable: 'NOT_INVESTIGABLE',
  InvalidIdentity: 'INVALID_IDENTITY',
  DatabaseUnavailable: 'DATABASE_UNAVAILABLE',
  ValidationFailed: 'VALIDATION_FAILED',
  NotFound: 'NOT_FOUND',
} as const

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly details: Record<string, unknown> | null

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null,
    details: Record<string, unknown> | null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.details = details
  }

  /** Whether retrying the same request could plausibly succeed. */
  get isRetryable(): boolean {
    return this.code === ErrorCode.DatabaseUnavailable
  }

  /**
   * Whether the failure is the user's current view being out of date.
   *
   * A review against a stale investigation is refused with `INVALID_TRANSITION` and
   * this message; the UI's response is to offer a re-investigation, not to show a
   * generic failure.
   */
  get isStaleEvidence(): boolean {
    return this.code === ErrorCode.InvalidTransition && /stale/i.test(this.message)
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  let code = 'HTTP_ERROR'
  let message = `Request failed with status ${response.status}`
  let requestId: string | null = null
  let details: Record<string, unknown> | null = null
  try {
    const body = (await response.json()) as ApiErrorBody
    if (body.error) {
      code = body.error.code
      message = body.error.message
      requestId = body.error.request_id ?? null
      details = body.error.details ?? null
    }
  } catch {
    // A body that is not the documented envelope -- a proxy error page, say. The status
    // and a generic message are still worth surfacing, so this falls through rather
    // than replacing the failure with a parse error.
  }
  return new ApiError(response.status, code, message, requestId, details)
}

/**
 * The identity headers.
 *
 * These are development labels, not authentication, and the UI must not pretend
 * otherwise: they can be set by anyone, and nothing authorises on them.
 */
let actor = { id: 'dev-reviewer', role: 'reviewer' }

export function setActor(id: string, role: string): void {
  actor = { id, role }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    'X-Actor-Id': actor.id,
    'X-Actor-Role': actor.role,
    ...extra,
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: headers((init?.headers as Record<string, string>) ?? {}),
  })
  if (!response.ok) {
    throw await toApiError(response)
  }
  return (await response.json()) as T
}

export const api = {
  capabilities: (): Promise<Capabilities> => request('/capabilities'),

  health: (): Promise<{ status: string; version: string }> => request('/health'),

  listCases(params: { status?: CaseStatus; limit?: number; offset?: number } = {}): Promise<CaseList> {
    const query = new URLSearchParams()
    // `exactOptionalPropertyTypes` means an omitted key and an explicitly undefined one
    // are different types, so the caller passes nothing rather than undefined when
    // there is no filter.
    if (params.status !== undefined) query.set('status', params.status)
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    const suffix = query.toString()
    return request(`/disputes${suffix ? `?${suffix}` : ''}`)
  },

  getCase: (id: string): Promise<CaseDetail> => request(`/disputes/${id}`),

  investigate: (id: string): Promise<CaseDetail['investigations'][number]> =>
    request(`/disputes/${id}/investigations`, { method: 'POST', body: '{}' }),

  attachEvidence: (
    id: string,
    body: {
      payments?: {
        external_id: string
        amount: { amount: string; currency: string }
        allocations?: {
          invoice_external_id: string
          amount: { amount: string; currency: string }
        }[]
      }[]
      usage_events?: {
        external_id: string
        dedupe_key: string
        metric_key: string
        occurred_at: string
        quantity: string
        unit: string
      }[]
      snapshots?: {
        natural_key: string
        evidence_type: string
        payload: Record<string, unknown>
      }[]
    },
  ): Promise<CaseDetail> => request(`/disputes/${id}/evidence`, { method: 'POST', body: JSON.stringify(body) }),

  recordReview: (
    id: string,
    body: {
      investigation_id: string
      target_type: ReviewTargetType
      target_id: string
      action: ReviewAction
      rationale: string
      amended_narrative?: string
    },
  ): Promise<ReviewResponse> =>
    request(`/disputes/${id}/review`, { method: 'POST', body: JSON.stringify(body) }),

  reopen: (id: string, reason: string): Promise<CaseDetail> =>
    request(`/disputes/${id}/reopen`, { method: 'POST', body: JSON.stringify({ reason }) }),

  /**
   * Deliberately absent: approve, adjustment, execute.
   *
   * Those move money and arrive with idempotency keys and separation of duties. Adding
   * a method here before the backend offers the route would produce a button that
   * cannot work, and a reviewer who expects it.
   */
}
