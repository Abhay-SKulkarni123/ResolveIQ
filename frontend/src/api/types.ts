/**
 * The API's response shapes, mirrored.
 *
 * Two decisions are deliberate and worth stating, because both have caused real bugs:
 *
 * 1. **Every amount is `string`.** The backend stores money as `NUMERIC` and returns it
 *    as text, precisely so that no JSON number -- which is a float in JavaScript --
 *    ever holds a monetary figure. A type of `number` here would invite exactly the
 *    precision loss the backend works to prevent.
 * 2. **The error envelope is a type, not a string.** The UI branches on `error.code`,
 *    so a missing code is a type error rather than a silent `undefined` comparison.
 */

/** A monetary amount as text, e.g. `"5021.86"`. Never parse this. */
export type AmountText = string

export interface MoneyResponse {
  amount: AmountText | null
  currency: string | null
  not_assessable_reason?: string | null
}

export interface EvidenceResponse {
  natural_key: string
  evidence_type: string
  content_hash: string
  /** Always sent; an evidence row with no snapshot would be unreadable. */
  snapshot: Record<string, unknown>
  captured_at: string | null
}

export interface ReviewResponse {
  id: string
  investigation_id: string
  target_type: 'FINDING' | 'HYPOTHESIS' | 'RESOLUTION_OPTION'
  target_id: string
  action: 'ACCEPT' | 'REJECT' | 'REQUEST_MORE_INFO' | 'AMEND'
  actor_id: string
  actor_role: string
  rationale: string
  amended_narrative?: string | null
  evidence_fingerprint_seen: string
  /**
   * The backend's own flag for "this review was recorded against evidence that has
   * since changed". It is the server's judgement, so the UI shows it rather than
   * re-deriving staleness from the fingerprint.
   */
  is_against_current_evidence: boolean
  created_at: string
}

/**
 * A recorded closing verdict -- mirrors the backend's `DecisionResponse`.
 *
 * Deliberately no `amount`: a verdict closes the case, and the idempotency phase
 * records any money movement separately with its own guards.
 */
export interface DecisionResponse {
  id: string
  investigation_id: string
  decision: 'RESOLVED' | 'REJECTED'
  actor_id: string
  actor_role: string
  rationale: string
  evidence_fingerprint_seen: string
  created_at: string
}

export interface FindingResponse {
  id: string
  code: string
  severity: string
  category: string
  narrative: string
  /** Always present on a finding, and still text: it is a ratio, not money. */
  confidence: AmountText
  supporting_evidence: string[]
  reviews: ReviewResponse[]
}

export interface HypothesisResponse {
  id: string
  hypothesis_code: string
  title: string
  narrative: string
  status: string
  likelihood: AmountText | null
  metric_key: string | null
  impact: MoneyResponse
  /** Why the impact figure is what it is. `null` when it could not be established. */
  impact_basis: string | null
  impact_trace: Record<string, unknown>
  supporting_evidence: string[]
  refuting_evidence: string[]
  reviews: ReviewResponse[]
}

export interface ResolutionOptionResponse {
  id: string
  option_type: string
  title: string
  rationale: string
  requires_human_approval: boolean
  /** The hypothesis this option answers, when it answers one. */
  hypothesis_code: string | null
  supporting_evidence: string[]
  reviews: ReviewResponse[]
}

export interface CalculationResponse {
  currency: string
  engine_version: string
  recorded_total: AmountText | null
  recalculated_total: AmountText
  difference?: AmountText | null
  outstanding?: AmountText | null
  net_adjustments?: AmountText | null
  allocated_payments?: AmountText | null
  /** The invoice as submitted, echoed back so a figure can be traced to its source. */
  invoice: Record<string, unknown>
  balance: Record<string, unknown>
  usage_summaries: Record<string, unknown>[]
  is_complete: boolean
  is_provisional: boolean
  unresolved_metrics: string[]
  trace: Record<string, unknown>
}

/**
 * A degradation notice.
 *
 * The backend models these as bare reason strings, not objects, so this is a string
 * and not a `{ stage, reason }` pair. It was once typed as an object here, which made
 * the UI render `undefined: undefined` for every degraded run it was ever shown.
 */
export type Degradation = string

export interface InvestigationResponse {
  id: string
  version: number
  status: string
  evidence_fingerprint: string
  is_stale: boolean
  /** When the run was superseded, if it was. */
  stale_at: string | null
  created_at: string
  summary: string
  provider_name: string
  model: string
  prompt_version: string
  engine_version: string
  evidence: EvidenceResponse[]
  calculation: CalculationResponse | null
  findings: FindingResponse[]
  hypotheses: HypothesisResponse[]
  resolution_options: ResolutionOptionResponse[]
  degradations: Degradation[]
}

export type CaseStatus =
  | 'OPEN'
  | 'INVESTIGATING'
  | 'AWAITING_REVIEW'
  | 'REOPENED'
  | 'RESOLVED'
  | 'REJECTED'

/**
 * One row of the dispute list -- mirrors the backend's `CaseSummaryResponse`.
 *
 * The backend deliberately omits narratives here (it says so in the schema docstring):
 * a list of fifty cases must not drag fifty prose blobs across the wire. So there is
 * no `description` on this type, and no `review_count` either -- the list carries the
 * counts a reviewer triages on (`investigation_count`, `finding_count`) and nothing
 * more. If you need a narrative, fetch the detail.
 *
 * `src/test/contract.test.ts` fails the build if this drifts from the live OpenAPI
 * schema, so these fields are not a matter of opinion.
 */
export interface CaseSummary {
  id: string
  external_id: string
  invoice_external_id: string
  contract_external_id: string | null
  status: CaseStatus
  severity: string
  is_stale: boolean
  evidence_fingerprint: string
  version: number
  created_at: string
  investigation_count: number
  current_investigation_version: number | null
  /** String money. `null` means "not assessable", which is not the same as zero. */
  outstanding: AmountText | null
  currency: string | null
  finding_count: number
}

/** One dispute in full -- mirrors the backend's `DisputeDetailResponse`. */
export interface CaseDetail extends CaseSummary {
  /** Present on the detail only. The list omits narratives by design. */
  description: string
  evidence: EvidenceResponse[]
  /**
   * The run the backend considers current. Prefer this over guessing from
   * `investigations`: it is the server's answer to "which run is live", and a
   * client that recomputes it can disagree.
   */
  current_investigation: InvestigationResponse | null
  /** Every run, newest last. */
  investigations: InvestigationResponse[]
  reviews: ReviewResponse[]
  /** Every recorded closing verdict, newest last. */
  decisions: DecisionResponse[]
}

export interface CaseList {
  items: CaseSummary[]
  total: number
  limit: number
  offset: number
}

/**
 * The kinds of change a case can record. A closed set, mirrored from the backend's
 * `AuditEventType`, so a UI switch over `event_type` is exhaustive rather than a chain
 * of string comparisons that silently falls through on a new value.
 */
export type AuditEventType =
  | 'CASE_CREATED'
  | 'EVIDENCE_ATTACHED'
  | 'INVESTIGATION_COMPLETED'
  | 'REVIEW_RECORDED'
  | 'DECISION_RECORDED'
  | 'CASE_REOPENED'

/**
 * One entry in a case's audit trail -- mirrors the backend's `AuditEventResponse`.
 *
 * The context fields are nullable rather than optional and present-as-null: opening a
 * case has no run, an automated action may have no actor, and a request without an
 * idempotency key has no correlation id. "Not recorded" is a fact the UI shows, so the
 * fields are always sent.
 *
 * `payload` is event-specific and passed through as stored; the server does not
 * reinterpret it, so this is a bag rather than one shape per event type.
 */
export interface AuditEventResponse {
  id: string
  event_type: AuditEventType
  dispute_id: string
  occurred_at: string
  investigation_id: string | null
  actor_id: string | null
  actor_role: string | null
  entity_version: number | null
  correlation_id: string | null
  payload: Record<string, unknown>
}

/**
 * A page of a case's audit trail -- mirrors the backend's `AuditHistoryResponse`.
 *
 * Same envelope as `CaseList`, but oldest-first: a trail read backwards is a stack,
 * not a story.
 */
export interface AuditHistory {
  items: AuditEventResponse[]
  total: number
  limit: number
  offset: number
}

export interface Capabilities {
  llm_provider: string
  llm_model: string
  prompt_version: string
  store: 'postgres' | 'memory'
  /** False means a restart loses every case. The UI must say so, not imply otherwise. */
  persistence_is_durable: boolean
  identity: {
    mode: string
    is_authentication: boolean
    actor_id: string
    actor_role: string
  }
  features: Record<string, boolean>
}

/** The verbs a reviewer may record. There is deliberately no `APPROVE`. */
export type ReviewAction = 'ACCEPT' | 'REJECT' | 'REQUEST_MORE_INFO' | 'AMEND'

/** The verdicts that close a case. Deliberately no `APPROVE`. */
export type DecisionKind = 'RESOLVED' | 'REJECTED'

export type ReviewTargetType = 'FINDING' | 'HYPOTHESIS' | 'RESOLUTION_OPTION'

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details?: Record<string, unknown> | null
    request_id: string
  }
}
