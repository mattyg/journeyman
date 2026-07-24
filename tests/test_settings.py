from freecad.llm_copilot.settings import Settings, load_settings, save_settings

class FakeParam:
    def __init__(self): self.s, self.b, self.i = {}, {}, {}
    def GetString(self, k, d=""): return self.s.get(k, d)
    def SetString(self, k, v): self.s[k] = v
    def GetBool(self, k, d=False): return self.b.get(k, d)
    def SetBool(self, k, v): self.b[k] = v
    def GetInt(self, k, d=0): return self.i.get(k, d)
    def SetInt(self, k, v): self.i[k] = v

def test_defaults_when_unset():
    s = load_settings(FakeParam())
    assert s.model == ""
    assert s.confirm_before_running is True
    assert s.auto_approve_loop is False
    assert s.max_auto_approved_steps == 5
    assert s.self_correction_attempts == 3

def test_save_then_load_roundtrips():
    p = FakeParam()
    save_settings(p, Settings(
        model="anthropic/claude-opus-4-8", api_key="sk-x", api_base="",
        confirm_before_running=False, auto_approve_loop=True,
        max_auto_approved_steps=8, self_correction_attempts=2))
    s = load_settings(p)
    assert s.model == "anthropic/claude-opus-4-8"
    assert s.api_key == "sk-x"
    assert s.confirm_before_running is False
    assert s.auto_approve_loop is True
    assert s.max_auto_approved_steps == 8
    assert s.self_correction_attempts == 2
