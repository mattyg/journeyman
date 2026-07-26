from freecad.journeyman.document_inspector import (
    DocumentDelta, DocumentState, as_document_state, structured_diff,
)


def _state(objects):
    return {"objects": objects}


def test_created_deleted_modified_partition():
    before = _state({"A": {"x": 1}, "B": {"y": 2}})
    after = _state({"A": {"x": 1}, "B": {"y": 3}, "C": {"z": 4}})
    delta = DocumentDelta(before, after)
    assert delta.created == ["C"]
    assert delta.deleted == []
    assert delta.modified == ["B"]


def test_changed_names_is_created_plus_modified_sorted():
    before = _state({"A": {"x": 1}, "B": {"y": 2}})
    after = _state({"A": {"x": 1}, "B": {"y": 3}, "C": {"z": 4}})
    assert DocumentDelta(before, after).changed_names == ["B", "C"]


def test_created_types_reads_type_of_new_objects_only():
    before = _state({"A": {"type": "Part::Box"}})
    after = _state({
        "A": {"type": "Part::Box"},
        "P": {"type": "PartDesign::Pad"}})
    assert DocumentDelta(before, after).created_types() == {"PartDesign::Pad"}


def test_structured_free_function_matches_delta_method():
    before = _state({"A": {"x": 1}})
    after = _state({"A": {"x": 2}})
    assert structured_diff(before, after) == DocumentDelta(
        before, after).structured()


def test_structured_reports_no_changes():
    same = _state({"A": {"x": 1}})
    assert DocumentDelta(same, same).structured() == (
        "No observable document changes.")


def test_structured_diff_names_changed_fields_without_repeating_values():
    before = _state({"Pad": {
        "type": "PartDesign::Pad",
        "properties": {"Length": 10, "ReferenceAxis": "very long old value"},
    }})
    after = _state({"Pad": {
        "type": "PartDesign::Pad",
        "properties": {"Length": 20, "ReferenceAxis": "very long new value"},
    }})
    text = structured_diff(before, after)
    assert text == "Modified: Pad (Length, ReferenceAxis)"
    assert "very long" not in text


def test_structured_labels_each_change_kind():
    before = _state({"A": {"x": 1}, "D": {"gone": True}})
    after = _state({"A": {"x": 2}, "N": {"new": True}})
    out = DocumentDelta(before, after).structured()
    assert "Created: N" in out
    assert "Deleted: D" in out
    assert "Modified: A" in out


def test_document_state_owns_delta_and_health_derivations():
    before = DocumentState(_state({"Old": {"type": "Part::Feature"}}))
    after = DocumentState(_state({"Pad": {
        "type": "PartDesign::Pad",
        "shape": {"solids": 1, "volume": 42.5, "valid": True},
    }}))
    assert after.delta_from(before).changed_names == ["Pad"]
    assert after.health() == (1, 42.5, [])
    assert "Created: Pad" in after.structured_diff_from(before)


def test_as_document_state_preserves_instances_and_adapts_mappings():
    state = DocumentState(_state({}))
    assert as_document_state(state) is state
    assert isinstance(as_document_state(_state({})), DocumentState)
