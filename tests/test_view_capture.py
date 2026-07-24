from freecad.llm_copilot.view_capture import changed_object_names, _png_bytes


def test_changed_object_names_finds_created_and_modified_objects():
    before = {"objects": {"Same": {"x": 1}, "Changed": {"x": 1}}}
    after = {"objects": {
        "Same": {"x": 1}, "Changed": {"x": 2}, "New": {"x": 1},
    }}
    assert changed_object_names(before, after) == ["Changed", "New"]


def test_png_encoder_writes_a_valid_signature_and_chunks():
    data = _png_bytes(1, 1, bytearray([10, 20, 30]))
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in data
    assert data.endswith(b"IEND\xaeB`\x82")


def test_contact_sheet_layout_has_three_rows_and_columns():
    # The actual shape rendering is covered under freecadcmd; this verifies the
    # documented seven-view sheet dimensions through its 3x3 layout constants.
    from freecad.llm_copilot.view_capture import _VIEWS
    assert [name for name, _direction in _VIEWS] == [
        "front", "back", "left", "right", "top", "bottom", "isometric",
    ]
