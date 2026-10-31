"""Write clauses a translator would drop are refused; SET after a node MERGE is folded in.

The single-part router hands a query part to one translator by the kind of
write it sees, and each compiles only its own clauses. A CREATE beside a
MERGE, or a SET/DELETE beside a FOREACH, used to vanish from the AQL without
an error (silent data loss); MERGE of a named path never bound the path (it is
bound now for a single MERGE of a node or one hop, tests/test_write_sequencing.py).
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str) -> str:
    return translate(cypher, mapping=mapping_bundle_for("lpg")).aql


@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        # TCK Merge1 [7] / Create3 [13]: the CREATEs were dropped.
        ("CREATE (:User) CREATE (:User) MERGE (:User)", "CREATE beside MERGE"),
        ("MERGE (t:User {id: 42}) CREATE (f:Doc) CREATE (t)-[:LIKES]->(f)", "CREATE beside MERGE"),
        ("CREATE (a:User), (b:User) MERGE (a)-[:FOLLOWS]->(b) RETURN count(a)", "CREATE beside MERGE"),
        ("MATCH (m:User) MERGE (n:User {id: 1}) DELETE m", "DELETE beside MERGE"),
        ("MERGE (n:User {id: 1}) REMOVE n.x", "REMOVE beside MERGE"),
        ("CREATE (a:User) FOREACH (x IN [1] | CREATE (:Doc {v: x}))", "CREATE beside FOREACH"),
        ("MATCH (a:User) SET a.x = 1 FOREACH (x IN [1] | CREATE (:Doc {v: x}))", "SET beside FOREACH"),
        ("MATCH (a:User) FOREACH (x IN [1] | SET a.y = x) DELETE a", "DELETE beside FOREACH"),
        ("MERGE (a:User {id: 1}) FOREACH (x IN [1] | CREATE (:Doc {v: x}))", "MERGE beside FOREACH"),
        # SET after MERGE that cannot join the UPSERT documents.
        ("MATCH (a:User) MERGE (a)-[:FOLLOWS]->(b:User) SET a.x = 1", "relationship MERGE"),
        ("MATCH (a:User) MERGE (n:User {id: 1}) SET a.x = 1", "property of the merged node"),
        ("MERGE (n:User {id: 1}) SET n.x = n.x + 1", "reading the merged node"),
        ("MERGE (n:User {id: 1}) SET n += {x: 1}", "property of the merged node"),
        ("MERGE (n:User {id: 1}) SET n:Doc", "property of the merged node"),
        ("MERGE p = (a:User {num: 1}) MERGE (b:User {num: 2}) RETURN p", "named path"),
        ("MERGE p = (a:User)-[:FOLLOWS]->(b:User)-[:FOLLOWS]->(c:User) RETURN p", "named path"),
        # An ON CREATE / ON MATCH item on another variable was written onto
        # the merged node or relationship. On a relationship MERGE's bound
        # endpoint it is now written to that node (TCK Merge6 [1], Merge7 [1]).
        ("MATCH (a:User) MERGE (n:User {id: 1}) ON MATCH SET a.x = 1", "does not write"),
        # FOREACH parts its compiler leaves out.
        (
            "UNWIND [1, 2] AS i MATCH (a:User) FOREACH (x IN [i] | SET a.y = x)",
            "UNWIND or CALL before FOREACH",
        ),
        ("OPTIONAL MATCH (a:User) FOREACH (x IN [1] | SET a.y = x)", "OPTIONAL MATCH before FOREACH"),
        (
            "MATCH (a:User) FOREACH (x IN [1] | SET a.y = x) FOREACH (z IN [2] | SET a.z = z)",
            "Several FOREACH",
        ),
        ("MATCH (a:User) FOREACH (x IN [1] | SET a.y = x) RETURN a", "RETURN after FOREACH"),
        ("MATCH (a:User) FOREACH (x IN [1] | SET a += {y: x})", "whole map or a label inside FOREACH"),
        # A FOREACH beside SET/DELETE after a WITH was left out.
        ("MATCH (a:User) WITH a SET a.x = 1 FOREACH (x IN [1] | CREATE (:Doc {v: x}))", "FOREACH beside"),
        ("MATCH (a:User) WITH a DELETE a FOREACH (x IN [1] | CREATE (:Doc {v: x}))", "FOREACH beside"),
        # OPTIONAL MATCH before a write was compiled as a MATCH.
        ("OPTIONAL MATCH (a:User) MERGE (n:Doc {id: 1}) RETURN a", "OPTIONAL MATCH before CREATE/MERGE"),
        ("OPTIONAL MATCH (a:User) CREATE (n:Doc {id: 1})", "OPTIONAL MATCH before CREATE/MERGE"),
    ],
)
def test_refused(cypher: str, message: str) -> None:
    with pytest.raises(CoreError, match=message) as err:
        _aql(cypher)
    assert err.value.code == "UNSUPPORTED"


def test_set_after_a_node_merge_joins_both_upsert_documents() -> None:
    aql = _aql("MERGE (n:User {id: 1}) SET n.x = $v RETURN n")
    assert "INSERT {type: @typeValue, id: 1, x: @v}" in aql
    assert "UPDATE {x: @v}" in aql


def test_set_after_merge_runs_after_on_create_and_on_match() -> None:
    aql = _aql("MERGE (n:User {id: 1}) ON CREATE SET n.x = 1 ON MATCH SET n.x = 2 SET n.x = 3")
    assert "INSERT {type: @typeValue, id: 1, x: 3}" in aql
    assert "UPDATE {x: 3}" in aql
