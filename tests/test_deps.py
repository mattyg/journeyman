import freecad.journeyman.deps as deps


def test_ensure_reports_true_when_importable(monkeypatch):
    monkeypatch.setattr(deps, "_can_import", lambda name: True)
    assert deps.ensure_deps() is True


def test_ensure_reports_false_when_missing(monkeypatch):
    monkeypatch.setattr(deps, "_can_import", lambda name: False)
    assert deps.ensure_deps() is False
