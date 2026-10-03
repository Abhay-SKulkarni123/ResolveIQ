"""The evidence bundle: immutability, canonical hashing, and the fingerprint.

The properties tested here are what make an investigation reproducible. If a snapshot can
be altered after hashing, a finding's citation points at content nobody verified; if
hashing depends on key order or whitespace, two runs over identical evidence produce
different fingerprints and staleness detection reports a change that never happened.

The fingerprint's order-independence gets its own test because it is the property most
likely to be broken by a well-meaning change: sorting inside the hash function is one
line, and its absence produces a bug that only appears when collection order varies.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from app.domain.evidence import (
    EvidenceBundle,
    EvidenceError,
    EvidenceItem,
    EvidenceType,
    canonical_json,
    content_hash_of,
    deep_freeze,
)

SNAPSHOT = {"metric_key": "api_calls", "quantity": "41234", "nested": {"b": 2, "a": 1}}


def item(key: str = "invoice:INV-1", snapshot: dict | None = None) -> EvidenceItem:
    return EvidenceItem.create(
        key, EvidenceType.INVOICE, SNAPSHOT if snapshot is None else snapshot
    )


class TestCanonicalJson:
    def test_key_order_does_not_change_the_serialisation(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_nested_key_order_does_not_either(self) -> None:
        assert canonical_json({"x": {"a": 1, "b": 2}}) == canonical_json({"x": {"b": 2, "a": 1}})

    def test_insertion_order_within_a_list_is_significant(self) -> None:
        """Order is only insignificant for keys. A list is a sequence and means something.

        Asserted because "just sort everything" is the obvious over-correction: sorting a
        list of usage events would make two genuinely different evidence bundles hash
        identically.
        """
        assert canonical_json(["b", "a"]) != canonical_json(["a", "b"])

    def test_whitespace_is_not_significant(self) -> None:
        assert canonical_json({"a": 1, "b": [1, 2]}) == canonical_json({"a": 1, "b": [1, 2]})

    def test_non_ascii_text_is_not_escaped(self) -> None:
        """A dispute description in any language must hash on its real characters."""
        assert "é" in canonical_json({"text": "café"})

    def test_output_is_valid_json(self) -> None:
        assert json.loads(canonical_json(SNAPSHOT)) == SNAPSHOT


class TestContentHash:
    def test_equal_content_hashes_equally(self) -> None:
        assert content_hash_of({"a": 1}) == content_hash_of({"a": 1})

    def test_reordered_keys_hash_equally(self) -> None:
        assert content_hash_of({"a": 1, "b": 2}) == content_hash_of({"b": 2, "a": 1})

    def test_different_content_hashes_differently(self) -> None:
        assert content_hash_of({"a": 1}) != content_hash_of({"a": 2})

    def test_the_algorithm_travels_with_the_value(self) -> None:
        assert content_hash_of({"a": 1}).startswith("sha256:")


class TestEvidenceItem:
    def test_hash_is_derived_not_supplied(self) -> None:
        assert item().content_hash == content_hash_of(SNAPSHOT)

    def test_a_supplied_hash_that_lies_is_rejected(self) -> None:
        """The guard must reject, not silently overwrite.

        This is the test that fails if the derivation is done before the comparison: an
        implementation that assigns then compares would accept a snapshot paired with a
        hash that does not describe it, which is precisely the case the check exists for.
        """
        with pytest.raises(EvidenceError, match="content_hash"):
            EvidenceItem(
                natural_key="invoice:INV-1",
                evidence_type=EvidenceType.INVOICE,
                snapshot=SNAPSHOT,
                content_hash="sha256:" + "0" * 64,
            )

    def test_a_correct_supplied_hash_is_accepted(self) -> None:
        supplied = content_hash_of(SNAPSHOT)
        built = EvidenceItem(
            natural_key="invoice:INV-1",
            evidence_type=EvidenceType.INVOICE,
            snapshot=SNAPSHOT,
            content_hash=supplied,
        )
        assert built.content_hash == supplied

    def test_it_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            item().snapshot = {"tampered": True}  # type: ignore[misc]

    def test_an_empty_key_is_rejected(self) -> None:
        with pytest.raises(EvidenceError, match="natural_key"):
            EvidenceItem.create("   ", EvidenceType.INVOICE, SNAPSHOT)

    def test_a_non_mapping_snapshot_is_rejected(self) -> None:
        with pytest.raises(EvidenceError, match="must be a mapping"):
            EvidenceItem.create("k", EvidenceType.INVOICE, ["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_an_unserialisable_snapshot_is_rejected(self) -> None:
        """A snapshot that cannot be canonicalised cannot be hashed, so it is refused."""
        with pytest.raises(EvidenceError, match="not JSON-serialisable"):
            EvidenceItem.create("k", EvidenceType.INVOICE, {"when": object()})

    def test_changing_the_snapshot_changes_the_hash(self) -> None:
        before = item(snapshot={"quantity": "1"}).content_hash
        after = item(snapshot={"quantity": "2"}).content_hash
        assert before != after


class TestEvidenceBundle:
    def test_items_are_ordered_by_natural_key(self) -> None:
        bundle = EvidenceBundle.of(
            [item("z:1"), item("a:1"), item("m:1")],
        )
        assert [i.natural_key for i in bundle.items] == ["a:1", "m:1", "z:1"]

    def test_fingerprint_does_not_depend_on_collection_order(self) -> None:
        """The property that lets staleness detection mean anything."""
        forwards = EvidenceBundle.of([item("a:1"), item("b:1"), item("c:1")])
        backwards = EvidenceBundle.of([item("c:1"), item("b:1"), item("a:1")])
        shuffled = EvidenceBundle.of([item("b:1"), item("c:1"), item("a:1")])
        assert forwards.fingerprint == backwards.fingerprint == shuffled.fingerprint

    def test_fingerprint_changes_when_content_changes(self) -> None:
        before = EvidenceBundle.of([item("a:1", {"quantity": "1"})])
        after = EvidenceBundle.of([item("a:1", {"quantity": "2"})])
        assert before.fingerprint != after.fingerprint

    def test_fingerprint_changes_when_a_key_is_added(self) -> None:
        one = EvidenceBundle.of([item("a:1")])
        two = EvidenceBundle.of([item("a:1"), item("b:1")])
        assert one.fingerprint != two.fingerprint

    def test_adding_the_same_evidence_twice_is_a_no_op(self) -> None:
        """§5.2: re-adding identical evidence must not change the fingerprint.

        Asserted as fingerprint equality rather than merely "does not raise", because a
        bundle that kept the duplicate would hash the pair twice and report a change that
        never happened — which would make every staleness check fire spuriously.
        """
        once = EvidenceBundle.of([item("a:1")])
        twice = EvidenceBundle.of([item("a:1"), item("a:1")])
        assert len(twice) == 1
        assert once.fingerprint == twice.fingerprint

    def test_the_same_key_with_different_content_is_rejected(self) -> None:
        """Two different records under one key would make a citation ambiguous."""
        with pytest.raises(EvidenceError, match="appears twice"):
            EvidenceBundle.of([item("a:1", {"quantity": "1"}), item("a:1", {"quantity": "2"})])

    def test_allowed_keys_is_the_single_source_of_truth(self) -> None:
        bundle = EvidenceBundle.of([item("a:1"), item("b:1")])
        assert bundle.allowed_keys == frozenset({"a:1", "b:1"})

    def test_membership_uses_the_allowed_keys(self) -> None:
        bundle = EvidenceBundle.of([item("a:1")])
        assert "a:1" in bundle
        assert "nope:1" not in bundle

    def test_of_type_filters_and_keeps_order(self) -> None:
        bundle = EvidenceBundle.of(
            [
                EvidenceItem.create("a:1", EvidenceType.INVOICE, SNAPSHOT),
                EvidenceItem.create("b:1", EvidenceType.CONTRACT_TERM, SNAPSHOT),
                EvidenceItem.create("c:1", EvidenceType.INVOICE, SNAPSHOT),
            ]
        )
        found = bundle.of_type(EvidenceType.INVOICE)
        assert [i.natural_key for i in found] == ["a:1", "c:1"]

    def test_get_returns_none_for_an_absent_key(self) -> None:
        assert EvidenceBundle.of([item("a:1")]).get("nope:1") is None

    def test_an_empty_bundle_is_valid(self) -> None:
        """Zero evidence is a legitimate state, and it hashes.

        The workflow refuses to build a request from one, but the bundle itself must not
        need a special case: an investigation with no evidence is a failure to be reported
        by the caller, not a broken value type.
        """
        assert len(EvidenceBundle.of([])) == 0
        assert EvidenceBundle.of([]).fingerprint == content_hash_of([])


class TestTheSnapshotIsActuallyImmutable:
    """`@dataclass(frozen=True)` stops rebinding a field, not editing what the field holds.

    These tests exist because a snapshot was originally deep-copied but left mutable, so
    `item.snapshot["rate"] = ...` changed a record whose `content_hash` had already been
    sealed at construction. The stored hash then described content the object no longer
    held — the precise failure ADR-006 exists to prevent, on the object a reviewer is
    supposed to be able to trust.
    """

    def test_a_nested_value_cannot_be_edited_through_the_item(self) -> None:
        item = EvidenceItem.create(
            "contract:CTR-1#term:api_calls",
            EvidenceType.CONTRACT_TERM,
            {"tiers": [{"up_to": "1000", "rate": "0.0005"}]},
        )
        with pytest.raises(TypeError):
            item.snapshot["tiers"][0]["rate"] = "0.9999"  # type: ignore[index]

    def test_a_list_cannot_be_appended_to_through_the_item(self) -> None:
        item = EvidenceItem.create(
            "contract:CTR-1#term:api_calls",
            EvidenceType.CONTRACT_TERM,
            {"tiers": [{"up_to": "1000", "rate": "0.0005"}]},
        )
        with pytest.raises(AttributeError):
            item.snapshot["tiers"].append({"up_to": "2000", "rate": "0.0004"})  # type: ignore[attr-defined]

    def test_the_content_hash_still_describes_the_snapshot(self) -> None:
        """The invariant itself: sealed hash == hash of what is actually stored."""
        item = EvidenceItem.create("invoice:INV-1", EvidenceType.INVOICE, {"stated_total": "28.40"})
        assert content_hash_of(item.snapshot) == item.content_hash

    def test_a_caller_mutating_their_own_dict_cannot_reach_the_item(self) -> None:
        source: dict[str, Any] = {"tiers": [{"rate": "0.0005"}]}
        item = EvidenceItem.create("contract:CTR-1", EvidenceType.CONTRACT_TERM, source)
        expected = item.content_hash
        source["tiers"][0]["rate"] = "0.9999"
        source["tiers"].append({"rate": "0.1"})
        assert content_hash_of(item.snapshot) == expected

    def test_freezing_does_not_change_the_hash_bytes(self) -> None:
        """A frozen snapshot must hash identically to the plain structure it came from.

        If `deep_freeze` altered the serialised form, every fingerprint would depend on
        whether an item had been frozen, and two equal bundles would disagree.
        """
        plain = {"b": [1, 2], "a": {"z": True, "y": None}}
        assert content_hash_of(deep_freeze(plain)) == content_hash_of(plain)

    def test_equal_content_frozen_two_ways_still_hashes_equal(self) -> None:
        one = EvidenceItem.create("k", EvidenceType.INVOICE, {"a": 1, "b": [1, 2]})
        two = EvidenceItem.create("k", EvidenceType.INVOICE, {"b": [1, 2], "a": 1})
        assert one.content_hash == two.content_hash
