"""ORDER BY referencing a RETURN projection alias.

Cypher allows ``RETURN size(r.roles) AS n ORDER BY n``, but AQL's ``SORT`` runs
before the ``RETURN`` that binds ``n``, so emitting ``SORT n`` makes AQL read
``n`` as a collection ("collection or view not found: n"). The alias is inlined
to its compiled expression instead (``SORT LENGTH(r.roles)``), referencing the
still-live FOR variables.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestOrderByAlias:
    def test_order_by_computed_alias(self, pg):
        out = translate(
            "MATCH (m:Movie)<-[r:ACTED_IN]-(:Person) "
            "RETURN m.title, size(r.roles) AS role_count ORDER BY role_count DESC LIMIT 3",
            mapping=pg,
        )
        # LENGTH() is never null, so no null-placement key precedes it.
        assert "SORT LENGTH(r.roles) DESC" in out.aql
        assert "role_count ==" not in out.aql and "SORT role_count" not in out.aql

    def test_order_by_renamed_property_alias(self, pg):
        out = translate(
            "MATCH (d:Person {name: 'Nancy Meyers'})-[:DIRECTED]->(m:Movie) "
            "RETURN m.title AS title, m.released AS year ORDER BY year",
            mapping=pg,
        )
        assert "SORT (m.released == null), m.released" in out.aql
        assert "year ==" not in out.aql and "SORT year" not in out.aql

    def test_order_by_node_only_alias(self, pg):
        out = translate(
            "MATCH (movie:Movie) "
            "RETURN movie.title, size(movie.tagline) AS tagline_length "
            "ORDER BY tagline_length DESC LIMIT 1",
            mapping=pg,
        )
        assert "SORT LENGTH(movie.tagline) DESC" in out.aql

    def test_order_by_plain_variable_unaffected(self, pg):
        # No alias involved → ORDER BY on a live var must be untouched.
        out = translate(
            "MATCH (m:Movie) RETURN m.title ORDER BY m.released DESC",
            mapping=pg,
        )
        assert "SORT (m.released == null) DESC, m.released DESC" in out.aql
