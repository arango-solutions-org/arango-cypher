"""``compute_statistics`` on polymorphic relationships and supertypes.

A relationship whose endpoint lists several labels (``domain: ["Post",
"Comment"]``) was looked up as a dict key: ``TypeError``, and the bundle had
no statistics at all (LDBC SNB, PG and LPG). Supertypes were counted as their
whole collection. The fake database answers the counting queries by their
shape; it does not parse AQL, so the live check is the SF0.1 run recorded in
the PR and ``tests/integration/test_schema_acquisition.py``.
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import MappingBundle

from arango_cypher.schema_acquire import compute_statistics
from tests.helpers.fake_arango import FakeDb

_TOTALS = {"post": 30, "comment": 20, "person": 5, "nodes": 90, "has_creator": 50, "reply_of": 20}
_LABELLED = {"Message": 50}


def _handler(query: str, bind_vars: dict[str, Any]) -> list[Any]:
    total = re.fullmatch(r"RETURN LENGTH\(`(\w+)`\)", query)
    if total:
        return [_TOTALS[total.group(1)]]
    if "@lv IN d[@lf]" in query:
        return [_LABELLED[bind_vars["lv"]]]
    raise AssertionError(f"unexpected AQL: {query!r}")


def _rel(edge: str, domain: Any, range_: Any) -> dict[str, Any]:
    return {"style": "DEDICATED_COLLECTION", "edgeCollectionName": edge, "domain": domain, "range": range_}


def _bundle(**overrides: Any) -> MappingBundle:
    physical = {
        "entities": {
            "Post": {"style": "COLLECTION", "collectionName": "post"},
            "Comment": {"style": "COLLECTION", "collectionName": "comment"},
            "Person": {"style": "COLLECTION", "collectionName": "person"},
            "Message": {"style": "UNION", "members": ["Post", "Comment"]},
        },
        "relationships": {
            "HAS_CREATOR": _rel("has_creator", ["Post", "Comment"], "Person"),
            "REPLY_OF": _rel("reply_of", "Comment", ["Post", "Comment"]),
        },
    }
    physical.update(overrides.pop("physical", {}))
    return MappingBundle(
        conceptual_schema=overrides.pop("conceptual", {"entities": [], "relationships": []}),
        physical_mapping=physical,
        metadata={},
    )


def test_a_list_valued_endpoint_counts_every_label() -> None:
    stats = compute_statistics(FakeDb(handler=_handler), _bundle())
    creator = stats["relationships"]["HAS_CREATOR"]
    assert creator["source_count"] == 50  # 30 posts + 20 comments
    assert creator["avg_in_degree"] == 10.0  # 50 messages over 5 people
    assert stats["relationships"]["REPLY_OF"]["target_count"] == 50


def test_conceptual_list_endpoints_are_read_too() -> None:
    bundle = _bundle(
        physical={
            "relationships": {
                "HAS_CREATOR": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "has_creator"}
            }
        },
        conceptual={
            "entities": [],
            "relationships": [
                {"type": "HAS_CREATOR", "fromEntity": ["Post", "Comment"], "toEntity": "Person"}
            ],
        },
    )
    assert (
        compute_statistics(FakeDb(handler=_handler), bundle)["relationships"]["HAS_CREATOR"]["source_count"]
        == 50
    )


def test_a_union_supertype_is_its_members_total() -> None:
    assert compute_statistics(FakeDb(handler=_handler), _bundle())["entities"]["Message"] == {
        "estimated_count": 50
    }


def test_a_multi_label_supertype_counts_its_label() -> None:
    bundle = _bundle(
        physical={
            "entities": {
                "Message": {
                    "style": "MULTI_LABEL",
                    "collectionName": "nodes",
                    "labelsField": "labels",
                    "labelValue": "Message",
                }
            },
            "relationships": {},
        }
    )
    # Not the collection's 90 documents: only those labelled Message.
    assert compute_statistics(FakeDb(handler=_handler), bundle)["entities"]["Message"] == {
        "estimated_count": 50
    }
