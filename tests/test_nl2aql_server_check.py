"""NL→AQL: the optional server check and the traversal-direction check.

Regression: the regex check passed queries the server refuses (SQL functions such
as DIVIDE or YEAR) and rejected valid ones (``FOR x IN <variable>``), and nothing
caught a traversal walked against its edge's direction.
"""

from __future__ import annotations

from arango_query_core.mapping import MappingBundle

from arango_cypher.nl2cypher._aql import _edge_endpoints, _traversal_direction_problems, nl_to_aql

PM = {
    "entities": {
        "Order": {"collectionName": "orders"},
        "Customer": {"collectionName": "customers"},
        "Person": {"collectionName": "people"},
    },
    "relationships": {
        "PLACED_BY": {"edgeCollectionName": "placed_by"},
        "KNOWS": {"edgeCollectionName": "knows"},
    },
}
CS = {
    "relationships": [
        {"type": "PLACED_BY", "fromEntity": "Order", "toEntity": "Customer"},
        {"type": "KNOWS", "fromEntity": "Person", "toEntity": "Person"},
    ]
}
BUNDLE = MappingBundle(conceptual_schema=CS, physical_mapping=PM, metadata={})
ENDPOINTS = _edge_endpoints(BUNDLE)
WRONG = "FOR c IN customers FOR o IN OUTBOUND c placed_by RETURN o"
RIGHT = "FOR c IN customers FOR o IN INBOUND c placed_by RETURN o"


class _Scripted:
    def __init__(self, *replies: str) -> None:
        self.replies, self.users = list(replies), []

    def generate(self, system: str, user: str) -> tuple[str, dict]:  # noqa: ARG002
        self.users.append(user)
        return f"```aql\n{self.replies.pop(0)}\n```", {}


def test_endpoints_resolve_collections_from_the_conceptual_schema():
    assert ENDPOINTS == {"placed_by": ("orders", "customers"), "knows": ("people", "people")}


def test_direction_problems():
    assert "use INBOUND" in _traversal_direction_problems(WRONG, ENDPOINTS)[0]
    assert _traversal_direction_problems('FOR o IN 1..1 OUTBOUND "customers/1" placed_by RETURN o', ENDPOINTS)
    assert _traversal_direction_problems(RIGHT, ENDPOINTS) == []
    assert _traversal_direction_problems("FOR p IN people FOR q IN INBOUND p knows RETURN q", ENDPOINTS) == []
    assert (
        _traversal_direction_problems(
            "LET c = DOCUMENT('customers/1') FOR o IN OUTBOUND c placed_by RETURN o", ENDPOINTS
        )
        == []
    )


def test_wrong_direction_is_sent_back_for_repair():
    provider = _Scripted(WRONG, RIGHT)
    assert nl_to_aql("orders per customer", mapping=BUNDLE, llm_provider=provider).aql == RIGHT
    assert "use INBOUND" in provider.users[1]


def test_server_error_drives_repair_and_replaces_the_regex():
    bad = "FOR o IN orders RETURN DIVIDE(o.total, 2)"
    good = "LET xs = (FOR o IN orders RETURN o.total) FOR x IN xs RETURN x / 2"  # the regex rejects this
    provider = _Scripted(bad, good)
    errors = {bad: "[1540] usage of unknown function 'DIVIDE()'"}
    assert nl_to_aql("half", mapping=BUNDLE, llm_provider=provider, explain=errors.get).aql == good
    assert "unknown function 'DIVIDE()'" in provider.users[1] and "DATE_YEAR()" in provider.users[1]
