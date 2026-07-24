# tests/test_api_reference.py
from freecad.llm_copilot.api_reference import _guide


def test_deletion_query_names_the_real_sketcher_methods():
    """Three failures in one run came from guessing this API.

    climbing-hanger-transcript-3 tried clearGeometry and deleteGeometry, then
    looked the API up and got a guide that warned about deletion indices
    without ever naming the method.
    """
    hits = _guide(
        "SketchObject methods for adding geometry and constraints, "
        "addGeometry signature, deleting geometry")
    text = "\n".join(hits)
    assert "delGeometry" in text
    assert "delConstraint" in text
    assert "no clearGeometry or deleteGeometry" in text


def test_short_deletion_queries_also_match():
    for query in ("deleting geometry", "clear a sketch", "remove constraint"):
        assert any("delGeometry" in hit for hit in _guide(query)), query


def test_guide_warns_that_indices_shift():
    text = "\n".join(_guide("delete sketch geometry"))
    assert "Indices shift" in text


def test_unrelated_query_does_not_match_the_sketch_entry():
    assert not any("delGeometry" in hit for hit in _guide("boolean fuse cut"))
