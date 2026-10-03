"""Evidence: the immutable, hashable snapshot a finding may cite.

docs/SYSTEM_DESIGN.md §5.2 makes the bundle immutable, and that single decision is what
makes an investigation reproducible. Two runs a week apart must reach the same
conclusions, or "the model changed its mind" becomes indistinguishable from "the data
changed underneath it". So each source record is copied at collection time, hashed, and
the hashes together form a ``fingerprint`` recorded on the investigation.

Findings cite ``natural_key`` strings (``invoice:INV-2026-03-0042``) rather than database
row ids. A row id is only meaningful to one database; a natural key survives being
quoted in a ticket, pasted into a screenshot, or read aloud in a review.

Two rules this module enforces, both of which the calculation path depends on:

* An evidence item is immutable once constructed, and its ``content_hash`` is derived
  from the snapshot rather than supplied. A caller cannot assert a hash that does not
  match its own content, which would let a tampered snapshot keep a trusted fingerprint.
* A ``natural_key`` is unique within a bundle. Two items claiming to be the same line of
  the same invoice would make a citation ambiguous, and an ambiguous citation cannot be
  checked.

Snapshots are plain JSON-compatible data. The model *reads* snapshots; the deterministic
engine reads typed value objects (:mod:`app.domain.billing`) and never parses a snapshot.
That asymmetry is deliberate — it is what stops free-form source text from reaching an
arithmetic path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

__all__ = [
    "EvidenceBundle",
    "EvidenceError",
    "EvidenceItem",
    "EvidenceType",
    "canonical_json",
    "content_hash_of",
]


class EvidenceError(ValueError):
    """Raised when evidence is structurally invalid.

    Raised at construction so a malformed record cannot be cited later. Distinct from a
    *citation* failure (:mod:`app.services.citations`), which is about a response naming
    a key that is not in the bundle — a well-formed key that should not have been used,
    rather than a key that could not exist.
    """


class EvidenceType(str, Enum):
    """The kinds of record a finding may cite. Fixed by docs/SYSTEM_DESIGN.md §5.1."""

    #: The invoice header, including its stated total.
    INVOICE = "INVOICE"
    #: One line of the invoice, identified by its line number.
    INVOICE_LINE = "INVOICE_LINE"
    #: Aggregated metered quantity for one metric over the invoice period.
    USAGE_SUMMARY = "USAGE_SUMMARY"
    #: One individual usage event.
    USAGE_EVENT = "USAGE_EVENT"
    #: A payment received.
    PAYMENT = "PAYMENT"
    #: The portion of a payment assigned to a particular invoice.
    PAYMENT_ALLOCATION = "PAYMENT_ALLOCATION"
    #: A priced term of a contract version — the rule a charge is supposed to follow.
    CONTRACT_TERM = "CONTRACT_TERM"
    #: The customer's own description of the dispute.
    DISPUTE_TEXT = "DISPUTE_TEXT"


def _thaw(value: Any) -> Any:
    """Convert a deep-frozen structure back into plain JSON-compatible data.

    :func:`deep_freeze` produces ``MappingProxyType`` and ``tuple`` values that
    ``json.dumps`` cannot serialise directly. Converting them back here means the bytes
    hashed are identical to those of the original ``dict``/``list`` structure, so
    freezing a snapshot cannot change its ``content_hash``.
    """
    if isinstance(value, Mapping):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(nested) for nested in value]
    return value


def deep_freeze(value: Any) -> Any:
    """Return ``value`` with every mapping and sequence made read-only, recursively.

    ``@dataclass(frozen=True)`` stops a caller rebinding ``item.snapshot``, but it does
    nothing to the ``dict`` inside. Without this, ``item.snapshot["rate"] = ...`` would
    silently change the record's content while ``content_hash`` — frozen at construction —
    kept describing the *old* content. That is precisely the failure ADR-006 exists to
    prevent: a fingerprint that no longer describes what it fingerprints, on the exact
    object a reviewer is supposed to be able to trust.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: deep_freeze(nested) for key, nested in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(nested) for nested in value)
    return value


def canonical_json(value: Any) -> str:
    """Serialise ``value`` so that equal content always yields equal bytes.

    Three things make JSON unsuitable for hashing without this: key order is preserved
    rather than sorted, insignificant whitespace varies, and non-ASCII characters may be
    escaped or not. Any of those would give two snapshots of identical content different
    hashes, and a fingerprint that changes when nothing did is a fingerprint nobody trusts.

    ``sort_keys`` orders keys, the tight separators remove whitespace, and
    ``ensure_ascii=False`` leaves text as UTF-8 so a dispute description in any language
    hashes on its actual characters.
    """
    return json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash_of(snapshot: Any) -> str:
    """SHA-256 of the canonical serialisation of ``snapshot``.

    Hex digest, lowercase, prefixed ``sha256:`` so the algorithm travels with the value
    and a future migration to a different hash cannot be mistaken for a match.
    """
    return "sha256:" + hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceItem:
    """One immutable, hashed source record.

    ``snapshot`` is a copy taken at collection time. It is never refreshed, so a
    re-run of the investigation sees what the first run saw.
    """

    #: Stable, human-readable identifier, e.g. ``invoice:INV-2026-03-0042#line:LI-0007``.
    natural_key: str
    evidence_type: EvidenceType
    #: The copied record, as JSON-compatible data.
    snapshot: Mapping[str, Any]
    #: SHA-256 over :func:`canonical_json` of ``snapshot``. Derived, never supplied.
    content_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.natural_key.strip():
            raise EvidenceError("evidence natural_key must not be empty")
        if not isinstance(self.snapshot, Mapping):
            raise EvidenceError(
                f"evidence {self.natural_key!r} snapshot must be a mapping, "
                f"got {type(self.snapshot).__name__}"
            )
        try:
            canonical_json(self.snapshot)
        except (TypeError, ValueError) as exc:
            raise EvidenceError(
                f"evidence {self.natural_key!r} snapshot is not JSON-serialisable: {exc}"
            ) from exc

        # Captured before it is overwritten. Comparing after the assignment would compare
        # the derived value with itself and the check could never fail, which is worse
        # than not having it: the test guarding it would pass for the wrong reason.
        supplied = self.content_hash
        derived = content_hash_of(self.snapshot)
        if supplied and supplied != derived:
            raise EvidenceError(
                f"evidence {self.natural_key!r} was given content_hash "
                f"{supplied!r} but its snapshot hashes to {derived!r}"
            )
        # object.__setattr__ because the dataclass is frozen: the hash is not an input,
        # it is a fact about the snapshot, and letting a caller pass one in would let an
        # altered snapshot keep a hash that no longer matches it.
        #
        # The snapshot is frozen *before* hashing, and the hash covers the frozen form, so
        # content_hash_of(self.snapshot) still equals self.content_hash afterwards. A
        # shallow copy would leave the caller able to change the content of a record whose
        # hash is already sealed.
        frozen = deep_freeze(dict(self.snapshot))
        object.__setattr__(self, "snapshot", frozen)
        object.__setattr__(self, "content_hash", content_hash_of(frozen))

    @classmethod
    def create(
        cls, natural_key: str, evidence_type: EvidenceType, snapshot: Mapping[str, Any]
    ) -> EvidenceItem:
        """Build an item, deriving ``content_hash`` from ``snapshot``."""
        return cls(natural_key=natural_key, evidence_type=evidence_type, snapshot=snapshot)


@dataclass(frozen=True)
class EvidenceBundle:
    """The complete set of evidence for one investigation.

    Immutable and ordered by ``natural_key``, so the fingerprint does not depend on the
    order evidence happened to be collected in. Two investigations over the same records
    produce the same fingerprint regardless of how they gathered them.
    """

    items: tuple[EvidenceItem, ...]
    #: SHA-256 over the sorted ``(natural_key, content_hash)`` pairs. Drives staleness
    #: detection and, later, whether a reviewer's approval is still valid (§9.2).
    fingerprint: str = field(default="")

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.items, key=lambda item: item.natural_key))

        # A key may appear more than once only with identical content. §5.2 requires
        # re-adding the same evidence to be a no-op, so the repeat is dropped rather
        # than rejected — otherwise an idempotent "add evidence" call would fail the
        # second time. A key carrying *different* content is a genuine conflict: the
        # first snapshot was taken under one reading of the source record and the second
        # under another, and a citation to that key could not be checked against either.
        seen: dict[str, str] = {}
        unique: list[EvidenceItem] = []
        for item in ordered:
            existing = seen.get(item.natural_key)
            if existing is None:
                seen[item.natural_key] = item.content_hash
                unique.append(item)
            elif existing != item.content_hash:
                raise EvidenceError(
                    f"evidence key {item.natural_key!r} appears twice in one bundle with "
                    f"different content ({existing} and {item.content_hash}); a citation to "
                    "it would be ambiguous"
                )

        deduplicated = tuple(unique)
        object.__setattr__(self, "items", deduplicated)
        object.__setattr__(self, "fingerprint", self._fingerprint_of(deduplicated))

    @staticmethod
    def _fingerprint_of(items: Sequence[EvidenceItem]) -> str:
        """SHA-256 over sorted ``(natural_key, content_hash)`` pairs.

        Sorted, so collection order cannot change the value. Deliberately built from the
        keys and hashes only, not the snapshots: the hashes already stand in for the
        content, and re-serialising the snapshots here would make a fingerprint
        computation fail for reasons that have nothing to do with it.
        """
        pairs = sorted({(item.natural_key, item.content_hash) for item in items})
        return content_hash_of([[key, digest] for key, digest in pairs])

    @classmethod
    def of(cls, items: Sequence[EvidenceItem]) -> EvidenceBundle:
        """Build a bundle, ordering items and deriving the fingerprint."""
        return cls(items=tuple(items))

    @property
    def allowed_keys(self) -> frozenset[str]:
        """Every key a finding is permitted to cite.

        The single source of truth for the allowlist. Validation reads this, so a key
        cannot be accepted by one check and rejected by another.
        """
        return frozenset(item.natural_key for item in self.items)

    def get(self, natural_key: str) -> EvidenceItem | None:
        """The item with this key, or ``None`` if the bundle does not contain it."""
        for item in self.items:
            if item.natural_key == natural_key:
                return item
        return None

    def of_type(self, evidence_type: EvidenceType) -> tuple[EvidenceItem, ...]:
        """Every item of one type, in ``natural_key`` order."""
        return tuple(item for item in self.items if item.evidence_type is evidence_type)

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, natural_key: object) -> bool:
        return isinstance(natural_key, str) and natural_key in self.allowed_keys
