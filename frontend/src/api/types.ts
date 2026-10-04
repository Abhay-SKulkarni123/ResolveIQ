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
  snapshot?: Record<string, unknown>
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
  created_at: string
}

export interface FindingResponse {
  id: string
  code: string
  severity: string
  category: string
  narrative: string
  confidence: AmountText | null
  supporting_evidence: string[]
  refuting_evidence?: string[]
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
  supporting_evidence: string[]
  refuting_evidence: string[]
  reviews: ReviewResponse[]
}

export interface ResolutionOptionResponse {
  id: string
  option_type: string
  title: string
  narrative: string
  rationale: string
  requires_human_approval: boolean
  supporting_evidence: string[]
  reviews: ReviewResponse[]
}

export interface CalculationResponse {
  currency: string
  engine_version: string
  recorded_total: AmountText
  recalculated_total: AmountText
  difference?: AmountText | null
  outstanding?: AmountText | null
  net_adjustments?: AmountText | null
  is_complete: boolean
  is_provisional: boolean
  unresolved_metrics: string[]
  trace: Record<string, unknown>
}

export interface Degradation {
  stage: string
  reason: string
}

export interface InvestigationResponse {
  id: string
  dispute_id: string
  version: number
  status: string
  evidence_fingerprint: string
  is_stale: boolean
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

export interface CaseSummary {
  id: string
  external_id: string
  invoice_external_id: string
  contract_external_id: string | null
  status: CaseStatus
  severity: string
  description: string
  created_at: string
  version: number
  is_stale: boolean
  evidence_fingerprint: string
  current_investigation_version: number | null
  review_count: number
}

export interface CaseDetail extends CaseSummary {
  evidence: EvidenceResponse[]
  investigations: InvestigationResponse[]
  reviews: ReviewResponse[]
  source_document: Record<string, unknown>
}

export interface CaseList {
  items: CaseSummary[]
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

export type ReviewTargetType = 'FINDING' | 'HYPOTHESIS' | 'RESOLUTION_OPTION'

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details?: Record<string, unknown> | null
    request_id: string
  }
}
