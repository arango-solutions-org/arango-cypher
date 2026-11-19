"""Variable-length relationships on a type-discriminated edge collection (docs/ldbc_benchmark_plan.md §3.5).

Every emitter checked only the *last* edge's type, so on an LPG edge
collection ``[:REPLY_OF*0..]`` walked through every relationship type — wrong
paths and an exponential blow-up (LDBC IS2 on LPG hit the 30 s cap) — and the
zero-hop row was dropped because ``r`` is null there.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.formatting import _guard_varlength_edge_types
from tests.helpers.mapping_fixtures import mapping_bundle_for

_TYPED = "FILTER r[@relTypeField] == @relTypeValue"


class TestPostPass:
    def test_a_single_hop_is_untouched(self):
        aql = f"FOR v, r IN 1..1 OUTBOUND s @@edges\n  {_TYPED}"
        assert _guard_varlength_edge_types(aql) == aql

    def test_a_variable_length_hop_is_pruned_at_other_types(self):
        out = _guard_varlength_edge_types(f"FOR v, r IN 1..3 OUTBOUND s @@edges\n  {_TYPED}")
        assert "@@edges PRUNE r != null AND r[@relTypeField] != @relTypeValue\n" in out
        assert _TYPED in out

    def test_prune_goes_before_options(self):
        out = _guard_varlength_edge_types(
            f"FOR v, r IN 1..3 ANY s @@edges OPTIONS {{indexHint: {{}}}}\n  {_TYPED}"
        )
        assert "@@edges PRUNE r != null AND r[@relTypeField] != @relTypeValue OPTIONS {indexHint: {}}" in out

    def test_zero_hops_let_the_null_edge_through(self):
        out = _guard_varlength_edge_types(f"FOR v, r IN 0..10 OUTBOUND s @@edges\n  {_TYPED}")
        assert "FILTER (r == null OR r[@relTypeField] == @relTypeValue)" in out

    def test_the_path_gets_an_edge_condition_an_index_can_serve(self):
        # A PRUNE keeps the (_from, type) index out; the path condition lets it in.
        out = _guard_varlength_edge_types(f"FOR v, r IN 1..3 OUTBOUND s @@edges\n  {_TYPED}")
        assert "FOR v, r, _vlp IN 1..3" in out
        assert "FILTER _vlp.edges[*][@relTypeField] ALL == @relTypeValue " + _TYPED in out

    def test_a_declared_path_is_reused(self):
        out = _guard_varlength_edge_types(f"FOR v, r, hop IN 1..3 OUTBOUND s @@edges\n  {_TYPED}")
        assert "FOR v, r, hop IN 1..3" in out
        assert "FILTER hop.edges[*][@relTypeField] ALL == @relTypeValue" in out

    def test_the_added_path_avoids_names_in_use(self):
        out = _guard_varlength_edge_types(
            f"FOR v, r IN 1..3 OUTBOUND s @@edges\n  {_TYPED}\n  LET _vlp = 1\n  LET _vlp2 = 2"
        )
        assert "FOR v, r, _vlp3 IN 1..3" in out

    def test_zero_hops_pass_the_path_condition(self):
        # A zero-length path has no edges, so ALL holds and the zero-hop row stays.
        out = _guard_varlength_edge_types(f"FOR v, r IN 0..10 OUTBOUND s @@edges\n  {_TYPED}")
        assert "FILTER _vlp.edges[*][@relTypeField] ALL == @relTypeValue FILTER (r == null OR" in out

    def test_a_dedicated_edge_collection_is_untouched(self):
        aql = "FOR v, r IN 1..3 OUTBOUND s @@knows\n  RETURN v"
        assert _guard_varlength_edge_types(aql) == aql

    def test_another_traversals_filter_is_not_borrowed(self):
        aql = (
            "FOR v, r IN 1..3 OUTBOUND s @@knows\n"
            "  FOR w, r2 IN 1..1 OUTBOUND v @@edges\n"
            "    FILTER r[@relTypeField] == @relTypeValue"
        )
        assert _guard_varlength_edge_types(aql) == aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (c:Comment {id: 1})-[:REPLY_OF*0..]->(p:Post) RETURN p.id",
        "MATCH (c:Comment {id: 1}) WITH c MATCH (c)-[:REPLY_OF*1..3]->(p:Post) RETURN p.id",
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) WHERE (a)-[:KNOWS*1..2]-(b) RETURN a.id, b.id",
    ],
    ids=["first-match", "bound-match", "pattern-predicate"],
)
def test_every_lpg_emitter_is_guarded(cypher):
    aql = translate(cypher, mapping=mapping_bundle_for("ldbc_lpg")).aql
    assert " PRUNE " in aql and "!= null AND" in aql, aql


def test_pg_variable_length_is_unchanged():
    aql = translate(
        "MATCH (a:Person {id: 1})-[:KNOWS*1..2]-(f:Person) RETURN f.id", mapping=mapping_bundle_for("ldbc_pg")
    ).aql
    assert "PRUNE" not in aql


@pytest.mark.parametrize("rng", ["*", "*0..", "*2.."])
def test_an_open_ended_range_warns_about_its_hop_cap(rng):
    q = translate(
        f"MATCH (c:Comment {{id: 1}})-[:REPLY_OF{rng}]->(p:Post) RETURN p.id",
        mapping=mapping_bundle_for("ldbc_pg"),
    )
    assert any("limited to 10 hops" in w["message"] for w in q.warnings), q.warnings


@pytest.mark.parametrize("rng", ["*1..3", "*..3", "*2"])
def test_a_bounded_range_does_not_warn(rng):
    q = translate(
        f"MATCH (c:Comment {{id: 1}})-[:REPLY_OF{rng}]->(p:Post) RETURN p.id",
        mapping=mapping_bundle_for("ldbc_pg"),
    )
    assert not any("hops" in w["message"] for w in q.warnings), q.warnings


def test_several_types_test_every_edge_against_the_list():
    aql = translate(
        "MATCH (a:Person {id: 1})-[:KNOWS|LIKES*1..2]->(x) RETURN x.id",
        mapping=mapping_bundle_for("ldbc_lpg"),
    ).aql
    assert "ALL IN @" in aql and "ALL == @" not in aql, aql
