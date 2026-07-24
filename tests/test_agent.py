# tests/test_agent.py
from freecad.llm_copilot.agent import Agent
from freecad.llm_copilot.types import ExecResult
from freecad.llm_copilot.llm_client import LLMProposal
from freecad.llm_copilot.settings import Settings

class FakeClient:
    def __init__(self, proposals): self.proposals, self.calls = proposals, []
    def complete(self, messages, settings):
        self.calls.append(messages)
        return self.proposals.pop(0)

class FakeExecutor:
    def __init__(self, results): self.results, self.undos = results, 0
    def run(self, app, script): return self.results.pop(0)
    def undo(self, app): self.undos += 1

def _settings(**kw):
    base = dict(model="m", api_key="", api_base="", confirm_before_running=True,
                auto_approve_loop=False, max_auto_approved_steps=5,
                self_correction_attempts=3)
    base.update(kw); return Settings(**base)

def _agent(client, executor, settings, snapshot="DOC"):
    return Agent(client=client, inspector=lambda app: snapshot,
                 executor=executor, app=object(), settings=settings)

def test_single_step_then_done():
    client = FakeClient([
        LLMProposal("add a box", "import Part", "", True),
        LLMProposal("", "", "Done — added a box.", False),
    ])
    ex = FakeExecutor([ExecResult(True, "", "")])
    intents = []
    out = _agent(client, ex, _settings()).send(
        "make a box", on_intent=lambda i: intents.append(i) or True,
        on_result=lambda r, s: None)
    assert intents == ["add a box"]
    assert "Done" in out

def test_gate_rejection_stops_without_executing():
    client = FakeClient([LLMProposal("add a box", "import Part", "", True)])
    ex = FakeExecutor([])
    out = _agent(client, ex, _settings()).send(
        "make a box", on_intent=lambda i: False, on_result=lambda r, s: None)
    assert "cancel" in out.lower()

def test_self_correction_on_error_then_success():
    client = FakeClient([
        LLMProposal("add a box", "bad", "", True),
        LLMProposal("fix it", "good", "", True),
        LLMProposal("", "", "Fixed.", False),
    ])
    ex = FakeExecutor([ExecResult(False, "", "Traceback: NameError"),
                       ExecResult(True, "", "")])
    out = _agent(client, ex, _settings(auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s: None)
    assert "Fixed" in out
    # the retry message must carry the traceback back to the model
    assert any("NameError" in str(m) for m in client.calls[-1])

def test_gives_up_after_self_correction_attempts():
    props = [LLMProposal("try", "bad", "", True) for _ in range(3)]
    client = FakeClient(props)
    ex = FakeExecutor([ExecResult(False, "", "err")] * 3)
    out = _agent(client, ex, _settings(self_correction_attempts=3,
                                       auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s: None)
    assert "couldn't" in out.lower() or "could not" in out.lower()

def test_stops_at_max_auto_approved_steps():
    # every proposal is a successful tool call -> would loop forever without cap
    props = [LLMProposal(f"s{i}", "ok", "", True) for i in range(10)]
    client = FakeClient(props)
    ex = FakeExecutor([ExecResult(True, "", "")] * 10)
    out = _agent(client, ex, _settings(auto_approve_loop=True,
                                       max_auto_approved_steps=2)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s: None)
    assert "pause" in out.lower() or "continue" in out.lower()
