# tests/test_agent.py
import threading

from freecad.llm_copilot.agent import Agent
from freecad.llm_copilot.agent import AgentCancelled
from freecad.llm_copilot.types import ExecResult
from freecad.llm_copilot.llm_client import LLMProposal
from freecad.llm_copilot.llm_client import LLMTimeoutError
from freecad.llm_copilot.settings import Settings

class FakeClient:
    def __init__(self, proposals): self.proposals, self.calls = proposals, []
    def complete(self, messages, settings):
        # Capture the value at call time; Agent intentionally mutates its
        # history after complete() returns.
        self.calls.append([dict(message) for message in messages])
        return self.proposals.pop(0)

class FakeExecutor:
    def __init__(self, results): self.results, self.undos = results, 0
    def run(self, app, script, **kwargs): return self.results.pop(0)
    def undo(self, app): self.undos += 1

def _settings(**kw):
    base = dict(model="m", api_key="", api_base="", confirm_before_running=True,
                auto_approve_loop=False, max_auto_approved_steps=5,
                self_correction_attempts=3, mandatory_verification=False)
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
        on_result=lambda r, s, i, p: None)
    assert intents == ["add a box"]
    assert "Done" in out

def test_gate_rejection_stops_without_executing():
    client = FakeClient([LLMProposal("add a box", "import Part", "", True)])
    ex = FakeExecutor([])
    out = _agent(client, ex, _settings()).send(
        "make a box", on_intent=lambda i: False,
        on_result=lambda r, s, i, p: None)
    assert "cancel" in out.lower()


def test_cancelled_response_is_discarded_before_script_runs():
    cancelled = threading.Event()

    class CancellingClient(FakeClient):
        def complete(self, messages, settings):
            proposal = super().complete(messages, settings)
            cancelled.set()
            return proposal

    client = CancellingClient([
        LLMProposal("add a box", "import Part", "", True),
    ])
    executor = FakeExecutor([])
    try:
        _agent(client, executor, _settings()).send(
            "make a box", on_intent=lambda i: True,
            on_result=lambda r, s, i, p: None,
            cancel_event=cancelled)
    except AgentCancelled:
        pass
    else:
        assert False, "expected AgentCancelled"
    assert executor.results == []
    assert client.calls

def test_self_correction_on_error_then_success():
    client = FakeClient([
        LLMProposal("add a box", "bad", "", True),
        LLMProposal("fix it", "good", "", True),
        LLMProposal("", "", "Fixed.", False),
    ])
    ex = FakeExecutor([ExecResult(False, "", "Traceback: NameError"),
                       ExecResult(True, "", "")])
    out = _agent(client, ex, _settings(auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s, i, p: None)
    assert "Fixed" in out
    # the retry message must carry the traceback back to the model
    assert any("NameError" in str(m) for m in client.calls[-1])

def test_gives_up_after_self_correction_attempts():
    props = [LLMProposal("try", "bad", "", True) for _ in range(3)]
    client = FakeClient(props)
    ex = FakeExecutor([ExecResult(False, "", "err")] * 3)
    out = _agent(client, ex, _settings(self_correction_attempts=3,
                                       auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s, i, p: None)
    assert "couldn't" in out.lower() or "could not" in out.lower()

def test_stops_at_max_auto_approved_steps():
    # every proposal is a successful tool call -> would loop forever without cap
    props = [LLMProposal(f"s{i}", "ok", "", True) for i in range(10)]
    client = FakeClient(props)
    ex = FakeExecutor([ExecResult(True, "", "")] * 10)
    out = _agent(client, ex, _settings(auto_approve_loop=True,
                                       max_auto_approved_steps=2)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s, i, p: None)
    assert "pause" in out.lower() or "continue" in out.lower()

def test_on_reasoning_fires_with_proposal_reasoning():
    client = FakeClient([
        LLMProposal("add a box", "import Part", "", True, reasoning="my thoughts"),
        LLMProposal("", "", "Done.", False),
    ])
    ex = FakeExecutor([ExecResult(True, "", "")])
    seen = []
    _agent(client, ex, _settings()).send(
        "box", on_intent=lambda i: True,
        on_result=lambda r, s, i, p: None,
        on_reasoning=lambda r: seen.append(r))
    assert "my thoughts" in seen

def test_on_reasoning_optional_and_skipped_when_empty():
    client = FakeClient([LLMProposal("", "", "Hi.", False)])  # no reasoning
    ex = FakeExecutor([])
    # omitting on_reasoning must not error
    out = _agent(client, ex, _settings()).send(
        "hi", on_intent=lambda i: True, on_result=lambda r, s, i, p: None)
    assert out == "Hi."

def test_script_output_fed_back_to_model():
    # first turn prints diagnostics; agent must include that stdout in the
    # message history so the model can read it on the next turn
    client = FakeClient([
        LLMProposal("inspect", "print('Vertex 0')", "", True),
        LLMProposal("", "", "Done.", False),
    ])
    ex = FakeExecutor([ExecResult(True, "Vertex 0 (1.0, 2.0)", "")])
    _agent(client, ex, _settings(auto_approve_loop=True)).send(
        "check", on_intent=lambda i: True,
        on_result=lambda r, s, i, p: None)
    # the second complete() call sees the printed output in the history
    assert any("Vertex 0 (1.0, 2.0)" in str(m) for m in client.calls[-1])


def test_stderr_and_console_diagnostics_are_fed_back_to_model():
    client = FakeClient([
        LLMProposal("diagnose", "pass", "", True),
        LLMProposal("", "", "Corrected.", False),
    ])
    result = ExecResult(
        True, "", "", stderr="python warning\n",
        console_warnings="attachment warning\n",
        console_errors="recompute failed\n")
    _agent(client, FakeExecutor([result]),
           _settings(auto_approve_loop=True)).send(
        "diagnose", lambda i: True, lambda r, s, i, p: None)
    context = str(client.calls[-1])
    assert "[standard error]" in context
    assert "[FreeCAD console warnings]" in context
    assert "[FreeCAD console errors]" in context
    assert "recompute failed" in context


def test_result_callback_receives_approved_intent_and_script():
    client = FakeClient([
        LLMProposal("Add a 10 mm cube", "print('made cube')", "", True),
        LLMProposal("", "", "Done.", False),
    ])
    result = ExecResult(True, "made cube", "")
    seen = []
    _agent(client, FakeExecutor([result]), _settings()).send(
        "cube", on_intent=lambda i: True,
        on_result=lambda r, snapshot, intent, script:
            seen.append((r, snapshot, intent, script)))
    assert seen == [
        (result, "DOC", "Add a 10 mm cube", "print('made cube')"),
    ]


def test_finish_ends_turn_with_summary():
    # a finish proposal (is_tool_call False, kind "finish") ends the turn and its
    # text is the reply; a script step runs first
    client = FakeClient([
        LLMProposal("make box", "pass", "", True),
        LLMProposal("", "", "Added the box — looks right?", False, kind="finish"),
    ])
    ex = FakeExecutor([ExecResult(True, "", "")])
    out = _agent(client, ex, _settings(auto_approve_loop=True)).send(
        "box", on_intent=lambda i: True, on_result=lambda r, s, i, p: None)
    assert out == "Added the box — looks right?"


def test_previous_turn_is_sent_to_model():
    client = FakeClient([
        LLMProposal("", "", "First answer.", False),
        LLMProposal("", "", "Second answer.", False),
    ])
    agent = _agent(client, FakeExecutor([]), _settings())
    agent.send("first question", lambda i: True, lambda r, s, i, p: None)
    agent.send("follow-up", lambda i: True, lambda r, s, i, p: None)
    second_call = client.calls[1]
    assert any(m["role"] == "assistant" and m["content"] == "First answer."
               for m in second_call)
    assert any("first question" in m["content"] for m in second_call)


def test_mandatory_verification_reprompts_before_finish():
    client = FakeClient([
        LLMProposal("make box", "pass", "", True),
        LLMProposal("", "", "Done too early", False, kind="finish"),
        LLMProposal("", "", "Verified done", False, kind="finish",
                    verified=True, evidence=("valid shape",)),
    ])
    out = _agent(
        client, FakeExecutor([ExecResult(True, "", "", True, "valid shape")]),
        _settings(mandatory_verification=True, auto_approve_loop=True)).send(
            "box", lambda i: True, lambda r, s, i, p: None)
    assert out == "Verified done"
    assert any("verification required" in m["content"].lower()
               for m in client.calls[-1])


def test_read_only_inspection_does_not_execute():
    client = FakeClient([
        LLMProposal("", "", "", False, kind="inspect", query="objects"),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([])
    out = _agent(client, executor, _settings()).send(
        "inspect", lambda i: True, lambda r, s, i, p: None)
    assert out == "Done"
    assert any("[inspection result]" in m["content"]
               for m in client.calls[-1])


def test_multiple_choice_answer_resumes_same_agent_turn():
    options = (
        {"id": "flush", "label": "Flush", "description": "Level with face."},
        {"id": "raised", "label": "Raised", "description": "Above face."},
    )
    client = FakeClient([
        LLMProposal(
            "", "", "", False, kind="question",
            question="Which mounting style?", options=options,
            recommended_option="flush"),
        LLMProposal("", "", "I will use the flush style.", False),
    ])
    seen = []
    out = _agent(client, FakeExecutor([]), _settings()).send(
        "make mount", lambda i: True, lambda r, s, i, p: None,
        on_question=lambda proposal: seen.append(proposal.question) or ["flush"])
    assert out == "I will use the flush style."
    assert seen == ["Which mounting style?"]
    assert "[user selection]" in str(client.calls[-1])
    assert "flush: Flush" in str(client.calls[-1])


def test_api_lookup_result_resumes_same_agent_turn():
    client = FakeClient([
        LLMProposal(
            "", "", "", False, kind="api_lookup",
            api_query="How do I make a box?", api_module="Part",
            api_symbol="makeBox"),
        LLMProposal("", "", "Use Part.makeBox.", False),
    ])
    inspector = lambda app: "DOC"
    inspector.api_lookup = (
        lambda app, query, module, symbol:
        "Installed FreeCAD 1.1\nSignature: Part.makeBox(length,width,height)")
    agent = Agent(
        client, inspector, FakeExecutor([]), object(), _settings())
    out = agent.send(
        "box", lambda i: True, lambda r, s, i, p: None)
    assert out == "Use Part.makeBox."
    assert "[installed-version API reference]" in str(client.calls[-1])
    assert "Part.makeBox" in str(client.calls[-1])


def test_api_error_triggers_automatic_lookup_before_retry():
    client = FakeClient([
        LLMProposal("bad API", "doc.XY_Plane", "", True),
        LLMProposal("correct API", "use origin", "", True),
        LLMProposal("", "", "Fixed.", False),
    ])
    inspector = lambda app: "DOC"
    inspector.api_lookup = (
        lambda app, query, module, symbol:
        "Origin planes belong to body.Origin.OriginFeatures")
    agent = Agent(
        client, inspector,
        FakeExecutor([
            ExecResult(False, "", "AttributeError: XY_Plane"),
            ExecResult(True, "", ""),
        ]),
        object(), _settings(auto_approve_loop=True, freecad_api_lookup=True))
    out = agent.send(
        "attach sketch", lambda i: True, lambda r, s, i, p: None)
    assert out == "Fixed."
    assert "[automatic installed-version API lookup]" in str(client.calls[1])
    assert "OriginFeatures" in str(client.calls[1])


def test_timeout_retry_continues_same_turn_without_duplicate_user_message():
    class TimeoutThenFinish:
        def __init__(self):
            self.calls = []

        def complete(self, messages, settings):
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                raise LLMTimeoutError("Timed out after 300s")
            return LLMProposal("", "", "Finished after retry.", False)

    client = TimeoutThenFinish()
    decisions = []
    out = _agent(client, FakeExecutor([]), _settings()).send(
        "make bracket", lambda i: True, lambda r, s, i, p: None,
        on_timeout=lambda message: decisions.append(message) or True)
    assert out == "Finished after retry."
    assert decisions == ["Timed out after 300s"]
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]


def test_timeout_stop_preserves_context_and_ends_turn():
    class AlwaysTimeout:
        def complete(self, messages, settings):
            raise LLMTimeoutError("Timed out after 300s")

    agent = _agent(AlwaysTimeout(), FakeExecutor([]), _settings())
    out = agent.send(
        "make bracket", lambda i: True, lambda r, s, i, p: None,
        on_timeout=lambda _message: False)
    assert "context have been preserved" in out
    assert "make bracket" in str(agent.messages)


def test_reviewed_plan_finish_does_not_require_an_extra_verify_tool_call():
    client = FakeClient([
        LLMProposal("make box", "pass", "", True, stage="additive"),
        LLMProposal("", "", "Reviewed and done", False, kind="finish",
                    reviewed_plan=True),
    ])
    out = _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(final_design_review=True, mandatory_verification=False,
                  auto_approve_loop=True)).send(
        "box", lambda i: True, lambda r, s, i, p: None)
    assert out == "Reviewed and done"
    assert len(client.calls) == 2


def test_final_review_reprompts_are_bounded():
    client = FakeClient([
        LLMProposal("make box", "pass", "", True, stage="additive"),
        LLMProposal("", "", "Done?", False, kind="finish"),
        LLMProposal("", "", "Still done?", False, kind="finish"),
    ])
    out = _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(final_design_review=True, mandatory_verification=False,
                  self_correction_attempts=1,
                  auto_approve_loop=True)).send(
        "box", lambda i: True, lambda r, s, i, p: None)
    assert "couldn't complete the required final review" in out
    assert len(client.calls) == 3


def test_rendered_views_are_added_as_multimodal_context():
    client = FakeClient([
        LLMProposal("make box", "pass", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    inspector = lambda app: "DOC"
    states = iter([
        {"objects": {}},
        {"objects": {"Box": {"type": "Part::Box"}}},
    ])
    inspector.state = lambda app, rich: next(states)
    agent = Agent(
        client, inspector=inspector,
        executor=FakeExecutor([ExecResult(True, "", "")]), app=object(),
        settings=_settings(rendered_views=True, auto_approve_loop=True),
        view_capture=lambda changed, strategy, limit: [
            {"label": "Box only", "data": "cG5n"}])
    agent.send("box", lambda i: True, lambda r, s, i, p: None)
    image_messages = [
        m for m in client.calls[-1] if isinstance(m["content"], list)]
    assert image_messages[0]["content"][2]["image_url"]["url"].endswith("cG5n")
    assert image_messages[0]["content"][1]["text"] == "Box only"


def test_unchanged_step_does_not_resend_snapshot_or_images():
    client = FakeClient([
        LLMProposal("measure", "print('10 mm')", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    captures = []
    agent = Agent(
        client, inspector=lambda app: "LARGE SNAPSHOT",
        executor=FakeExecutor([ExecResult(True, "10 mm", "")]), app=object(),
        settings=_settings(rendered_views=True, auto_approve_loop=True),
        view_capture=lambda *args: captures.append(args) or [])
    agent.send("measure", lambda i: True, lambda r, s, i, p: None)
    feedback = client.calls[-1][-1]["content"]
    assert "[document unchanged]" in feedback
    assert "[new snapshot]" not in feedback
    assert "LARGE SNAPSHOT" not in feedback
    assert captures == []


def test_context_callback_receives_only_new_messages_for_each_call():
    client = FakeClient([
        LLMProposal("make box", "pass", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    batches = []
    _agent(client, FakeExecutor([ExecResult(True, "", "")]),
           _settings(auto_approve_loop=True)).send(
        "box", lambda i: True, lambda r, s, i, p: None,
        on_context=lambda messages: batches.append(list(messages)))
    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert "[request]\nbox" in batches[0][0]["content"]
    assert any("[executed OK]" in message["content"]
               for message in batches[1])


def test_structured_plan_is_required_before_execution_and_ledger_is_sent():
    planned = dict(
        strategy="part_design", stage="sketch",
        plan=("Create constrained profile", "Pad profile"),
        plan_step=1,
        success_criteria=("One valid solid",))
    client = FakeClient([
        LLMProposal("edit immediately", "bad", "", True),
        LLMProposal("Create the profile", "pass", "", True, **planned),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")])
    out = _agent(
        client, executor,
        _settings(structured_cad_planning=True,
                  design_ledger_context=True,
                  auto_approve_loop=True)).send(
        "make a part", lambda i: True, lambda r, s, i, p: None)
    assert out == "Done"
    assert not executor.results
    assert any("[structured CAD plan required]" in str(call)
               for call in client.calls)
    assert "[design ledger]" in str(client.calls[-1])


def test_user_images_are_sent_in_initial_multimodal_message():
    client = FakeClient([LLMProposal("", "", "I see it.", False)])
    out = _agent(client, FakeExecutor([]), _settings()).send(
        "What is this?", lambda i: True, lambda r, s, i, p: None,
        user_images=[{"name": "part.png", "data": "YWJj"}])
    assert out == "I see it."
    content = client.calls[0][0]["content"]
    assert content[0]["type"] == "text"
    assert "[request]\nWhat is this?" in content[0]["text"]
    assert content[1] == {"type": "text",
                          "text": "User attachment: part.png"}
    assert content[2]["image_url"]["url"].endswith("YWJj")
