"""EXISTS / COUNT pattern-shorthand subqueries: ``exists{ (pattern) }``.

openCypher's newer form omits the ``MATCH`` inside the braces
(``exists{(p)-[:R]->(m)}``); the in-repo grammar needs an explicit reading
clause, so the parser inserts an implicit ``MATCH`` before parsing (string
literals excepted). A *bare* named target correlates to an outer binding
(``_sq_v._id == m._id``); a labeled target stays a fresh subquery-local binding.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestPatternShorthand:
    def test_count_shorthand_in_return(self, pg):
        out = translate(
            "MATCH (p:Person) "
            "RETURN p.name, count{(p)<-[:FOLLOWS]-(:Person)} AS followers "
            "ORDER BY followers DESC LIMIT 1",
            mapping=pg,
        )
        assert "LENGTH(FOR" in out.aql  # count{} → LENGTH subquery

    def test_exists_shorthand_in_where(self, pg):
        out = translate(
            "MATCH (d:Person)-[:DIRECTED]->(:Movie) "
            "WHERE NOT exists{ (d)-[:REVIEWED]->(:Movie) } RETURN d.name LIMIT 3",
            mapping=pg,
        )
        assert "(NOT (LENGTH(FOR" in out.aql

    def test_correlated_bare_target(self, pg):
        # `m` is bound outside → subquery correlates by _id, not by re-binding m
        out = translate(
            "MATCH (p:Person)-[:PRODUCED]->(m:Movie) "
            "WHERE exists{(p)-[:ACTED_IN]->(m)} RETURN p.name LIMIT 3",
            mapping=pg,
        )
        sub = out.aql.split("LENGTH(FOR")[1]
        assert "_sq_m_1._id == m._id" in sub
        assert "FOR m," not in sub  # m not re-bound in subquery

    def test_labeled_target_is_fresh_local(self, pg):
        # (f:Person) is a fresh subquery-local binding, not a correlation.
        out = translate(
            "MATCH (u:Person) WHERE NOT EXISTS { MATCH (u)-[:FOLLOWS]->(f:Person) } RETURN u.name",
            mapping=pg,
        )
        assert "_sq_v._id ==" not in out.aql

    def test_string_literal_not_rewritten(self, pg):
        # A literal containing `exists{(` must not get an implicit MATCH injected.
        out = translate(
            "MATCH (m:Movie) WHERE m.tagline = 'exists{(x)}' RETURN m.title",
            mapping=pg,
        )
        assert "MATCH (x)" not in out.aql
        assert "'exists{(x)}'" in out.aql


class TestSingleNodeSubquery:
    """Single-node EXISTS{}/COUNT{} bodies (no relationship chain).

    Previously rejected with "Subquery MATCH requires a relationship
    pattern"; now a labelled node compiles to a collection scan and a bare
    correlated node to a single-element probe over the outer binding.
    """

    def test_labeled_exists_scans_collection(self, pg):
        out = translate(
            "MATCH (p:Person) WHERE exists { (m:Movie) WHERE m.released > 2000 } RETURN p.name",
            mapping=pg,
        )
        assert "LENGTH(FOR _sq_m IN " in out.aql
        assert "> 0)" in out.aql
        assert "_sq_m.released" in out.aql

    def test_count_labeled_node(self, pg):
        out = translate(
            "MATCH (p:Person) RETURN p.name, count { (m:Movie) } AS c",
            mapping=pg,
        )
        assert "c: LENGTH(FOR _sq_m IN " in out.aql

    def test_correlated_bare_outer_node(self, pg):
        # `(p)` re-uses the outer binding → the WHERE on the outer variable,
        # over one row.
        out = translate(
            "MATCH (p:Person) WHERE exists { (p) WHERE p.born > 1970 } RETURN p.name",
            mapping=pg,
        )
        assert "LENGTH(FOR _sq_row IN [1] FILTER (p.born != null AND p.born > 1970) RETURN 1)" in out.aql

    def test_inline_properties(self, pg):
        out = translate(
            "MATCH (p:Person) WHERE exists { (m:Movie {released: 1999}) } RETURN p.name",
            mapping=pg,
        )
        assert "m.released == 1999" in out.aql

    def test_two_subqueries_use_distinct_binds(self, pg):
        # Collision-safe collection binds across sibling subqueries.
        out = translate(
            "MATCH (p:Person) RETURN count { (m:Movie) } AS a, "
            "count { (d:Movie) WHERE d.released > 2000 } AS b",
            mapping=pg,
        )
        assert out.bind_vars["@tailCollection"] == out.bind_vars["@tailCollection2"] == "movies"

    def test_anonymous_unlabeled_node_rejected(self, pg):
        from arango_query_core import CoreError

        with pytest.raises(CoreError) as exc:
            translate("MATCH (p:Person) WHERE exists { () } RETURN p.name", mapping=pg)
        assert exc.value.code == "UNSUPPORTED"


class TestAggregationSubquery:
    """`MATCH … WITH count(*) … [WHERE having]` subquery bodies.

    Requires the grammar to accept `WITH` inside `EXISTS{}`/`COUNT{}` (the
    `oC_SubqueryBody` rule was extended + parser regenerated). The counting
    existential lowers to `COLLECT WITH COUNT INTO <alias>` + a post-COLLECT
    HAVING filter; unsupported WITH shapes are refused, never mis-compiled.
    """

    def test_exists_with_count_having(self, pg):
        out = translate(
            "MATCH (p:Person) WHERE exists { "
            "MATCH (p)-[:ACTED_IN]->(m) WITH p, count(*) AS c WHERE c > 3 RETURN true "
            "} RETURN p.name",
            mapping=pg,
        )
        # Grouped by the outer p: no group (and no row) when p acted in nothing.
        assert (
            "COLLECT WITH COUNT INTO _sq_c FILTER _sq_c > 0 FILTER (_sq_c != null AND _sq_c > 3)" in out.aql
        )
        assert "(LENGTH(FOR _sq_m, _sq_r IN" in out.aql  # target is a fresh loop var
        assert "> 0)" in out.aql

    def test_exists_with_count_no_having(self, pg):
        out = translate(
            "MATCH (p:Person) WHERE exists { "
            "MATCH (p)-[:ACTED_IN]->(m) WITH count(*) AS c RETURN true "
            "} RETURN p.name",
            mapping=pg,
        )
        # No key: one row even when nothing matched, as in Cypher.
        assert "COLLECT WITH COUNT INTO _sq_c RETURN 1" in out.aql

    def test_count_subquery_with_aggregation(self, pg):
        out = translate(
            "MATCH (p:Person) RETURN p.name, count { "
            "MATCH (p)-[:ACTED_IN]->(m) WITH p, count(*) AS c WHERE c > 1 RETURN true "
            "} AS x",
            mapping=pg,
        )
        assert "x: LENGTH(FOR _sq_m, _sq_r IN" in out.aql
        assert "COLLECT WITH COUNT INTO _sq_c FILTER _sq_c > 0" in out.aql

    def test_reject_distinct_in_with(self, pg):
        from arango_query_core import CoreError

        with pytest.raises(CoreError) as exc:
            translate(
                "MATCH (p:Person) WHERE exists { "
                "MATCH (p)-[:ACTED_IN]->(m) WITH DISTINCT count(*) AS c RETURN true "
                "} RETURN p.name",
                mapping=pg,
            )
        assert exc.value.code == "UNSUPPORTED"

    def test_reject_group_by_local_var(self, pg):
        from arango_query_core import CoreError

        with pytest.raises(CoreError) as exc:
            translate(
                "MATCH (p:Person) WHERE exists { "
                "MATCH (p)-[:ACTED_IN]->(m) WITH m, count(*) AS c RETURN true "
                "} RETURN p.name",
                mapping=pg,
            )
        assert exc.value.code == "UNSUPPORTED"

    def test_reject_update_clause_in_subquery(self, pg):
        # SET is not part of oC_SubqueryBody → correct parse-time rejection
        # (openCypher marks the update-in-existential form as an error).
        from arango_query_core import CoreError

        with pytest.raises(CoreError):
            translate(
                "MATCH (p:Person) WHERE exists { MATCH (p)-[:ACTED_IN]->(m) SET m.x = 1 } RETURN p.name",
                mapping=pg,
            )
