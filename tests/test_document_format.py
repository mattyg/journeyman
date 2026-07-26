from freecad.journeyman.document_inspector import format_state, _is_noise


def _state():
    return {
        "document": "Unnamed",
        "selection": [],
        "objects": {
            "Body": {
                "type": "PartDesign::Body", "label": "Body",
                "origin_features": ["XY_Plane", "XZ_Plane"],
            },
            "Sketch": {
                "type": "Sketcher::SketchObject", "label": "Profile",
                "body": "Body", "fully_constrained": False,
                "solver": "Under-constrained: 2 DoF",
                "depends_on": ["XY_Plane"], "used_by": ["Pad"],
            },
            "Pad": {
                "type": "PartDesign::Pad", "label": "Pad", "body": "Body",
                "shape": {
                    "null": False, "valid": True, "solids": 1, "shells": 1,
                    "faces": 14, "edges": 36, "vertices": 24,
                    "volume": 12483.2, "area": 4200.0,
                    "cylinder_diameters": [6.0, 12.0],
                },
                "bbox": [40.0, 20.0, 15.0],
                "properties": {"Length": 15.0, "Reversed": False},
                "depends_on": ["Sketch"], "used_by": ["Body"],
            },
        },
    }


def test_format_state_is_flat_text_not_json():
    text = format_state(_state())
    assert "{" not in text and '":' not in text
    assert text.startswith("Document: Unnamed")


def test_format_state_reports_shape_health_and_geometry():
    text = format_state(_state())
    assert "Pad (PartDesign::Pad) body=Body" in text
    assert "solids=1" in text and "faces=14" in text
    assert "volume=12483.2" in text
    assert "bbox=40x20x15" in text
    assert "cylinder diameters: 6, 12" in text


def test_format_state_keeps_sketch_health_signals():
    text = format_state(_state())
    assert "NOT fully constrained" in text
    assert "solver: Under-constrained: 2 DoF" in text
    assert "origin features: XY_Plane, XZ_Plane" in text


def test_format_state_renders_links_compactly():
    text = format_state(_state())
    assert "<- Sketch" in text
    assert "-> Body" in text


def test_format_state_shows_a_differing_label_only():
    text = format_state(_state())
    assert "label='Profile'" in text   # Sketch's label differs from its name
    assert "label='Pad'" not in text   # Pad's does not, so it is not repeated


def test_format_state_flags_invalid_and_inspection_errors():
    state = _state()
    state["objects"]["Pad"]["shape"]["valid"] = False
    assert "INVALID" in format_state(state)
    state["objects"]["Pad"]["shape"] = {"inspection_error": "boom"}
    assert "shape could not be inspected: boom" in format_state(state)


def test_format_state_handles_empty_and_absent_documents():
    assert format_state({"document": None}) == "No active document."
    empty = format_state({"document": "Unnamed", "objects": {}})
    assert "(empty — no objects yet)" in empty


def test_format_state_is_substantially_smaller_than_json():
    import json
    text = format_state(_state())
    as_json = json.dumps(_state(), indent=2, sort_keys=True)
    assert len(text) < len(as_json) / 2


def test_noise_filter_drops_chrome_but_keeps_zero_values():
    assert _is_noise("Visibility", True)
    assert _is_noise("Placement", "...")
    assert _is_noise("Label2", "")
    # A zero Length is a real defect, not noise.
    assert not _is_noise("Length", 0)
    assert not _is_noise("Reversed", False)
    assert not _is_noise("Length", 15.0)
