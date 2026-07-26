from freecad.journeyman.document_inspector import DocumentDelta, structured_diff


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
