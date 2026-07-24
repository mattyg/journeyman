# tests/test_agent.py
import threading

from freecad.llm_copilot.agent import Agent, _model_history
from freecad.llm_copilot.agent import AgentCancelled
from freecad.llm_copilot.types import ExecResult
from freecad.llm_copilot.llm_client import LLMProposal
from freecad.llm_copilot.llm_client import LLMTimeoutError
from freecad.llm_copilot.settings import Settings
from freecad.llm_copilot import cad_workflow

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


def test_tool_callback_reports_script_before_execution():
    client = FakeClient([
        LLMProposal("add native feature", "pass", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    seen = []
    _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings()).send(
            "make it", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool=lambda tool, summary, details:
            seen.append((tool, summary, details)))
    assert seen == [(
        "run_freecad_script", "add native feature", "pass")]


def test_tool_callback_reports_inspection_and_api_lookup():
    client = FakeClient([
        LLMProposal(
            "", "", "", False, kind="inspect", query="Sketch.Support"),
        LLMProposal(
            "", "", "", False, kind="api_lookup",
            api_query="Pad length property", api_module="PartDesign",
            api_symbol="Pad"),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    inspector = lambda _app: "DOC"
    inspector.inspect = lambda _app, query: "inspection:" + query
    inspector.api_lookup = lambda _app, q, m, s: f"api:{m}.{s}:{q}"
    seen = []
    Agent(
        client, inspector, FakeExecutor([]), object(), _settings()).send(
            "inspect", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool=lambda tool, summary, details:
            seen.append((tool, summary, details)))
    assert seen == [
        ("inspect_document", "Sketch.Support", "Sketch.Support"),
        ("lookup_freecad_api", "PartDesign.Pad", "Pad length property"),
    ]


def test_on_tool_result_reports_inspection_and_api_lookup_results():
    client = FakeClient([
        LLMProposal(
            "", "", "", False, kind="inspect", query="Sketch.Support"),
        LLMProposal(
            "", "", "", False, kind="api_lookup",
            api_query="Pad length property", api_module="PartDesign",
            api_symbol="Pad"),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    inspector = lambda _app: "DOC"
    inspector.inspect = lambda _app, query: "inspection:" + query
    inspector.api_lookup = lambda _app, q, m, s: f"api:{m}.{s}:{q}"
    seen = []
    agent = Agent(
        client, inspector, FakeExecutor([]), object(), _settings())
    agent.send(
        "inspect", lambda _intent: True,
        lambda _r, _s, _i, _p: None,
        on_tool_result=lambda tool, summary, content:
        seen.append((tool, summary, content)))
    assert seen == [
        ("inspect_document", "Sketch.Support",
         "[inspection result]\ninspection:Sketch.Support"),
        ("lookup_freecad_api", "PartDesign.Pad",
         "[installed-version API reference]\napi:PartDesign.Pad:"
         "Pad length property"),
    ]
    # The callback content is exactly what was appended to the model history.
    user_feedback = [
        m["content"] for m in agent.messages if m["role"] == "user"]
    assert seen[0][2] in user_feedback
    assert seen[1][2] in user_feedback

def test_gate_rejection_stops_without_executing():
    client = FakeClient([LLMProposal("add a box", "import Part", "", True)])
    ex = FakeExecutor([])
    tool_results = []
    out = _agent(client, ex, _settings()).send(
        "make a box", on_intent=lambda i: False,
        on_result=lambda r, s, i, p: None,
        on_tool_result=lambda tool, summary, content:
        tool_results.append((tool, summary, content)))
    assert "cancel" in out.lower()
    assert tool_results == [(
        "run_freecad_script", "add a box",
        "Not executed — declined by user.")]


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


def test_cancel_after_execution_records_result_in_history():
    cancelled = threading.Event()

    class CancellingExecutor(FakeExecutor):
        def run(self, app, script, **kwargs):
            result = super().run(app, script, **kwargs)
            cancelled.set()
            return result

    client = FakeClient([LLMProposal("add a box", "import Part", "", True)])
    agent = _agent(
        client, CancellingExecutor([ExecResult(True, "box made", "")]),
        _settings(auto_approve_loop=True))
    try:
        agent.send(
            "make a box", on_intent=lambda i: True,
            on_result=lambda r, s, i, p: None,
            cancel_event=cancelled)
    except AgentCancelled:
        pass
    else:
        assert False, "expected AgentCancelled"
    text = str(agent.messages)
    assert "[step executed, then cancelled by user]" in text
    assert "box made" in text
    # The recorded outcome precedes the cancellation note.
    roles = [
        (m["role"], m["content"]) for m in agent.messages
        if "cancelled by user" in m["content"]
        or m["content"] == "Cancelled by user."]
    assert roles[0][0] == "user"
    assert roles[1] == ("assistant", "Cancelled by user.")

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


def test_retry_exhaustion_keeps_full_diagnostics_in_history():
    client = FakeClient([LLMProposal("try", "bad", "", True)])
    result = ExecResult(
        False, "partial output", "Traceback: NameError",
        stderr="deprecation warning\n")
    agent = _agent(
        client, FakeExecutor([result]),
        _settings(self_correction_attempts=1, auto_approve_loop=True))
    out = agent.send(
        "box", on_intent=lambda i: True, on_result=lambda r, s, i, p: None)
    assert "couldn't complete" in out
    feedback = [
        m["content"] for m in agent.messages
        if m["role"] == "user" and "[script failed]" in m["content"]]
    assert len(feedback) == 1
    assert "partial output" in feedback[0]
    assert "deprecation warning" in feedback[0]
    assert "NameError" in feedback[0]
    # The diagnostics precede the assistant's give-up summary.
    index = agent.messages.index(
        next(m for m in agent.messages
             if m["role"] == "user" and m["content"] == feedback[0]))
    assert agent.messages[index + 1] == {
        "role": "assistant", "content": out}


def test_successful_script_source_is_scrubbed_by_default():
    client = FakeClient([
        LLMProposal("add a box", "import Part", "", True),
        LLMProposal("", "", "Done.", False),
    ])
    agent = _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True))
    agent.send("box", on_intent=lambda i: True,
               on_result=lambda r, s, i, p: None)
    text = str(agent.messages)
    assert "(executed intent) add a box" in text
    assert "import Part" not in text


def test_keep_script_history_retains_script_source():
    client = FakeClient([
        LLMProposal("add a box", "import Part", "", True),
        LLMProposal("", "", "Done.", False),
    ])
    agent = _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True, keep_script_history=True))
    agent.send("box", on_intent=lambda i: True,
               on_result=lambda r, s, i, p: None)
    text = str(agent.messages)
    assert "(intent) add a box" in text
    assert "import Part" in text
    assert "(executed intent)" not in text

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
        {"objects": {
            "Body": {"type": "PartDesign::Body"},
            "Sketch": {
                "type": "Sketcher::SketchObject", "body": "Body"},
            "Pad": {"type": "PartDesign::Pad", "body": "Body"},
        }},
    ])
    inspector.state = lambda app, rich: next(states)
    agent = Agent(
        client, inspector=inspector,
        executor=FakeExecutor([ExecResult(True, "", "")]), app=object(),
        settings=_settings(rendered_views=True, auto_approve_loop=True),
        view_capture=lambda changed, strategy, limit: [
            {"label": "Pad only", "data": "cG5n"}])
    agent.send("box", lambda i: True, lambda r, s, i, p: None)
    image_messages = [
        m for m in client.calls[-1] if isinstance(m["content"], list)]
    assert image_messages[0]["content"][2]["image_url"]["url"].endswith("cG5n")
    assert image_messages[0]["content"][1]["text"] == "Pad only"


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
    feedback = client.calls[-1][-2]["content"]
    assert "[document unchanged]" in feedback
    assert "[new snapshot]" not in feedback
    assert "LARGE SNAPSHOT" not in feedback
    assert client.calls[-1][-1]["content"] == (
        "[current document]\nLARGE SNAPSHOT")
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
    assert len(batches[0]) == 2
    assert "[request]\nbox" in batches[0][0]["content"]
    assert batches[0][1]["content"].startswith("[current document]\n")
    assert any("[executed OK]" in message["content"]
               for message in batches[1])
    assert batches[1][-1]["content"].startswith("[current document]\n")


def test_legacy_history_drops_superseded_snapshots_ledgers_and_scripts():
    messages = [
        {"role": "user", "content": (
            "[document snapshot]\nOLD\n\n[request]\nmake it")},
        {"role": "assistant", "content": (
            "(intent) create\n(script)\nSECRET SOURCE")},
        {"role": "user", "content": (
            "[executed OK]\n[document diff]\nCreated: Box\n"
            "[new snapshot]\nOLD FULL STATE\n"
            "[design ledger]\nOLD PLAN")},
    ]
    compact = _model_history(messages)
    text = str(compact)
    assert "[request]\\nmake it" in text
    assert "OLD FULL STATE" not in text
    assert "OLD PLAN" not in text
    assert "SECRET SOURCE" not in text


def test_missing_plan_is_advisory_and_ledger_is_sent():
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
    executor = FakeExecutor([ExecResult(True, "", ""), ExecResult(True, "", "")])
    out = _agent(
        client, executor,
        _settings(structured_cad_planning=True,
                  design_ledger_context=True,
                  auto_approve_loop=True)).send(
        "make a part", lambda i: True, lambda r, s, i, p: None)
    assert out == "Done"
    # A missing plan is metadata: it no longer discards the script, but the
    # model is told about it alongside the step that ran.
    assert any("[workflow advisories]" in str(call) for call in client.calls)
    assert any("ordered feature-level plan" in str(call)
               for call in client.calls)
    assert "[design ledger]" in str(client.calls[-1])


def test_blocking_assumption_requires_question_before_execution():
    row = {
        "id": "width", "name": "Width", "value": 20.0, "unit": "mm",
        "source": "photo estimate", "confidence": "low",
        "consequence": "high", "if_wrong": "scale is wrong",
        "status": "unverified", "evidence": "",
    }
    confirmed = dict(
        row, value=25.0, status="user_confirmed",
        evidence="User selected 25 mm")
    client = FakeClient([
        LLMProposal("build", "pass", "", True, assumptions=(row,)),
        LLMProposal("", "", "", False, kind="question",
                    question="Choose width",
                    options=(
                        {"id": "20", "label": "20 mm", "description": "narrow"},
                        {"id": "25", "label": "25 mm", "description": "wide"},
                    )),
        LLMProposal("build", "pass", "", True, assumptions=(confirmed,)),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")])
    out = _agent(
        client, executor,
        _settings(assumption_ledger=True, auto_approve_loop=True)).send(
            "make it", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_question=lambda _proposal: ["25"])
    assert out == "Done"
    assert not executor.results
    assert any("[assumption clarification required]" in str(call)
               for call in client.calls)


def test_missing_assumption_ledger_is_corrected_before_execution():
    client = FakeClient([
        LLMProposal("build", "pass", "", True),
        LLMProposal("build", "pass", "", True, assumptions=()),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 2)
    out = _agent(
        client, executor,
        _settings(assumption_ledger=True, auto_approve_loop=True)).send(
            "make it", lambda _intent: True,
            lambda _r, _s, _i, _p: None)
    assert out == "Done"
    # A first step carrying no ledger is asked for one, but still runs: the
    # request is what matters, and refusing costs a whole turn.
    assert any("provide an assumption ledger with this step" in str(call)
               for call in client.calls)


def test_malformed_assumption_ledger_is_corrected_before_execution():
    """The reactive gate still catches a ledger that is present but invalid."""
    bad = (_assumption(confidence="certain"),)  # not one of high/medium/low
    client = FakeClient([
        LLMProposal("build", "pass", "", True, assumptions=bad),
        LLMProposal("build", "pass", "", True, assumptions=(_assumption(),)),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 2)
    out = _agent(
        client, executor,
        _settings(assumption_ledger=True, auto_approve_loop=True)).send(
            "make it", lambda _intent: True, lambda _r, _s, _i, _p: None)
    assert out == "Done"
    assert any("[workflow advisories]" in str(call) for call in client.calls)
    assert any("invalid confidence" in str(call) for call in client.calls)


def _feature(status="planned", evidence=""):
    return {
        "id": "lower_bend", "description": "Bent lower clip",
        "status": status, "evidence": evidence,
    }


def test_replica_blocked_feature_is_advisory_but_blocks_the_finish():
    client = FakeClient([
        LLMProposal(
            "make flat approximation", "pass", "", True,
            observed_features=(_feature("blocked"),)),
        LLMProposal(
            "model the bend", "pass", "", True,
            observed_features=(_feature(),)),
        LLMProposal(
            "verify bend", "pass", "", True,
            observed_features=(
                _feature("implemented", "Measured 25 degree bend"),)),
        LLMProposal(
            "", "", "Done", False, kind="finish",
            fidelity_met=True, fidelity_omissions=()),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 3)
    out = _agent(
        client, executor,
        _settings(fidelity_target="replica", auto_approve_loop=True)).send(
            "replicate hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None)
    assert out == "Done"
    # Difficulty is reported as an advisory rather than discarding the script;
    # the completion gate is what refuses to finish on an unresolved feature.
    assert any("difficulty is not permission to omit" in str(call)
               for call in client.calls)


def test_replica_finish_rejected_while_feature_is_only_planned():
    client = FakeClient([
        LLMProposal(
            "start bend", "pass", "", True,
            observed_features=(_feature(),)),
        LLMProposal(
            "", "", "Done early", False, kind="finish",
            fidelity_met=True, fidelity_omissions=()),
        LLMProposal(
            "finish bend", "pass", "", True,
            observed_features=(
                _feature("implemented", "Measured 25 degree bend"),)),
        LLMProposal(
            "", "", "Done", False, kind="finish",
            fidelity_met=True, fidelity_omissions=()),
    ])
    executor = FakeExecutor([
        ExecResult(True, "", ""), ExecResult(True, "", "")])
    out = _agent(
        client, executor,
        _settings(fidelity_target="replica", auto_approve_loop=True)).send(
            "replicate hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None)
    assert out == "Done"
    assert any("[replica fidelity required]" in str(call)
               for call in client.calls)


def test_replica_omission_requires_explicit_question_round():
    omitted = _feature(
        "user_approved_omission", "User selected omit bend")
    options = (
        {"id": "model", "label": "Model bend", "description": "Full replica"},
        {"id": "omit", "label": "Omit bend", "description": "Simpler model"},
    )
    client = FakeClient([
        LLMProposal(
            "omit bend", "pass", "", True,
            observed_features=(omitted,)),
        LLMProposal(
            "", "", "", False, kind="question",
            question="May I omit the bend?", options=options),
        LLMProposal(
            "build approved flat form", "pass", "", True,
            observed_features=(omitted,)),
        LLMProposal(
            "", "", "Done", False, kind="finish",
            fidelity_met=True, fidelity_omissions=("lower_bend",)),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 2)
    out = _agent(
        client, executor,
        _settings(fidelity_target="replica", auto_approve_loop=True)).send(
            "replicate hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_question=lambda _proposal: ["omit"])
    assert out == "Done"
    assert not executor.results


def test_fidelity_advisory_shows_user_the_text_sent_to_the_model():
    client = FakeClient([
        LLMProposal(
            "start bend", "pass", "", True,
            observed_features=(_feature("blocked", "too hard"),)),
        LLMProposal(
            "start bend", "pass", "", True,
            observed_features=(
                _feature("implemented", "Measured 25 degree bend"),)),
        LLMProposal(
            "", "", "Done", False, kind="finish",
            fidelity_met=True, fidelity_omissions=()),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 2)
    out = _agent(
        client, executor,
        _settings(fidelity_target="replica", auto_approve_loop=True)).send(
            "replicate hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None)
    assert out == "Done"
    # The objection is named in the advisory the model receives with its step,
    # rather than discarding a working script before it runs.
    advisory = [
        str(call) for call in client.calls
        if "[workflow advisories]" in str(call)]
    assert advisory
    assert "difficulty is not permission to omit" in advisory[-1]


def _gate_feedback_pairs(client, executor, settings, **send_kw):
    """Run a turn, returning (shown_to_user, sent_to_model) for gate blocks."""
    shown = []
    agent = _agent(client, executor, settings)
    agent.send(
        "do it", lambda _intent: True, lambda _r, _s, _i, _p: None,
        on_tool_result=lambda tool, summary, content:
        shown.append(content), **send_kw)
    sent = [
        message["content"] for message in agent.messages
        if message["role"] == "user"]
    return shown, sent


def test_every_gate_rejection_reaches_the_user_verbatim():
    """A gate that rejects a step must never leave the transcript silent."""
    client = FakeClient([
        # Rejected: not part_design.
        LLMProposal("try part", "pass", "", True, strategy="part"),
        LLMProposal("native", "pass", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown, sent = _gate_feedback_pairs(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True))
    gate_blocks = [text for text in shown if text.startswith("[")]
    assert gate_blocks, "the rejected step produced no user-visible feedback"
    for block in gate_blocks:
        assert block in sent


def test_part_workbench_step_is_rolled_back_and_rebuilt_natively():
    states = iter([
        {"objects": {}},
        {"objects": {"Box": {"type": "Part::Box", "label": "Box"}}},
        {"objects": {}},
        {"objects": {
            "Body": {"type": "PartDesign::Body", "label": "Body"},
            "Sketch": {
                "type": "Sketcher::SketchObject", "label": "Sketch",
                "body": "Body"},
            "Pad": {
                "type": "PartDesign::Pad", "label": "Pad", "body": "Body"},
        }},
    ])

    def inspector(_app):
        return "DOC"

    inspector.state = lambda _app, _rich: next(states)
    client = FakeClient([
        LLMProposal(
            "shortcut", "pass", "", True, strategy="part_design"),
        LLMProposal(
            "native pad", "pass", "", True, strategy="part_design"),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([
        ExecResult(True, "", ""), ExecResult(True, "", "")])
    agent = Agent(
        client, inspector, executor, object(),
        _settings(auto_approve_loop=True))
    reported = []
    out = agent.send(
        "make box", lambda _intent: True,
        lambda r, s, i, p: reported.append((r, i)))
    assert out == "Done"
    assert executor.undos == 1
    assert any("[Part Design violation" in str(call)
               for call in client.calls)
    # The rolled-back step must still be reported to the UI as a result.
    assert len(reported) == 2
    assert reported[0][1] == "shortcut"
    assert reported[0][0].rolled_back is True
    assert reported[1][1] == "native pad"
    assert reported[1][0].rolled_back is False


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


# --- Direct handler tests (seams exposed by extracting Agent.send) ---------
from freecad.llm_copilot.agent import _Turn, LOOP


def _bare_agent(settings):
    return _agent(FakeClient([]), FakeExecutor([]), settings)


def test_handle_completion_gates_on_missing_verification():
    agent = _bare_agent(_settings(mandatory_verification=True))
    turn = _Turn()
    turn.executed_steps = 1
    proposal = LLMProposal("", "", "all done", False, verified=False)
    signal = agent._handle_completion(proposal, turn)
    assert signal is LOOP
    assert turn.completion_retries == 1
    assert "[verification required]" in str(agent.messages[-1])


def test_handle_completion_returns_text_when_satisfied():
    agent = _bare_agent(_settings(mandatory_verification=False))
    turn = _Turn()
    turn.executed_steps = 1
    proposal = LLMProposal("", "", "all done", False)
    assert agent._handle_completion(proposal, turn) == "all done"


def test_handle_question_rejects_too_few_options():
    agent = _bare_agent(_settings())
    turn = _Turn()
    proposal = LLMProposal(
        "", "", "", False, kind="question", question="Which?",
        options=({"id": "a", "label": "A", "description": ""},))
    signal = agent._handle_question(
        proposal, turn, on_question=lambda p: [], check_cancelled=lambda: None)
    assert signal is LOOP
    assert turn.question_retries == 1
    assert "[invalid ask_user call]" in str(agent.messages[-1])


# --- DocumentAccess adaptation (the seam that replaced four inline shims) ----
from freecad.llm_copilot.agent import DocumentAccess


class _StubAgent:
    def __init__(self, settings):
        self.settings = settings


def test_document_access_uses_rich_kwarg_when_inspector_accepts_it():
    seen = {}

    def inspector(app, rich=False):
        seen["rich"] = rich
        return "SNAP"

    access = DocumentAccess(
        inspector, app="APP",
        agent=_StubAgent(_settings(rich_snapshot=True)))
    assert access.snapshot() == "SNAP"
    assert seen["rich"] is True


def test_document_access_falls_back_to_bare_test_double():
    access = DocumentAccess(
        lambda app: "SNAP", app="APP",
        agent=_StubAgent(_settings()))
    assert access.snapshot() == "SNAP"


def test_document_access_reads_settings_live_from_agent():
    def inspector(app, rich=False):
        return "rich" if rich else "plain"

    stub = _StubAgent(_settings(rich_snapshot=False))
    access = DocumentAccess(inspector, app="APP", agent=stub)
    assert access.snapshot() == "plain"
    # A mid-conversation settings swap must be reflected immediately.
    stub.settings = _settings(rich_snapshot=True)
    assert access.snapshot() == "rich"


def test_document_access_prefers_inspector_attributes_for_state_and_inspect():
    inspector = lambda app: "SNAP"
    inspector.state = lambda app, rich: {"objects": {"via": "attr"}}
    inspector.inspect = lambda app, query: f"inspected:{query}"
    inspector.api_lookup = lambda app, q, m, s: f"api:{m}.{s}"
    access = DocumentAccess(
        inspector, app="APP", agent=_StubAgent(_settings()))
    assert access.state() == {"objects": {"via": "attr"}}
    assert access.inspect("edges") == "inspected:edges"
    assert access.api_lookup("q", "Part", "Box") == "api:Part.Box"


def _occ_failure(line=13):
    return ExecResult(
        False, "",
        f'  File "<llm_script>", line {line}, in <module>\n'
        "Part.OCCError: 19Standard_NullObject BRepCheck_Analyzer::Init() "
        "- NULL shape\n")


def test_same_feature_failing_the_same_way_stops_at_the_cap():
    """The climbing-hanger loop: six PlatePad attempts, one error, no progress."""
    client = FakeClient([
        LLMProposal(f"pad the plate ({n})", f"pad = {n}", "", True, plan_step=1)
        for n in range(6)])
    executor = FakeExecutor([_occ_failure() for _ in range(6)])
    out = _agent(
        client, executor,
        _settings(auto_approve_loop=True, feature_retry_cap=2,
                  self_correction_attempts=99)).send(
            "model a hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None)
    assert "couldn't build this feature" in out
    assert "failed 3 times with the same error" in out
    assert "OCCError" in out
    # Stopped at the cap: three attempts consumed, the rest untouched.
    assert len(executor.results) == 3


def test_repeated_failure_nudge_reaches_model_and_user():
    client = FakeClient([
        LLMProposal("pad", "a", "", True, plan_step=1),
        LLMProposal("pad again", "b", "", True, plan_step=1),
        LLMProposal("", "", "Gave up", False, kind="finish"),
    ])
    executor = FakeExecutor([_occ_failure(), _occ_failure()])
    shown = []
    agent = _agent(
        client, executor,
        _settings(auto_approve_loop=True, feature_retry_cap=5,
                  self_correction_attempts=99))
    agent.send(
        "model a hanger", lambda _intent: True, lambda _r, _s, _i, _p: None,
        on_tool_result=lambda tool, summary, content: shown.append(content))
    nudges = [t for t in shown if t.startswith("[repeated failure")]
    assert len(nudges) == 1
    assert "OCCError" in nudges[0]
    sent = [m["content"] for m in agent.messages if m["role"] == "user"]
    assert nudges[0] in sent


def test_a_different_error_on_the_same_step_resets_the_counter():
    client = FakeClient([
        LLMProposal("pad", "a", "", True, plan_step=1),
        LLMProposal("pad", "b", "", True, plan_step=1),
        LLMProposal("pad", "c", "", True, plan_step=1),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([
        _occ_failure(),
        ExecResult(False, "", "ValueError: bad radius\n"),
        _occ_failure(),
    ])
    out = _agent(
        client, executor,
        _settings(auto_approve_loop=True, feature_retry_cap=2,
                  self_correction_attempts=99)).send(
            "model it", lambda _intent: True, lambda _r, _s, _i, _p: None)
    # Never three of a kind in a row, so the cap never fires.
    assert out == "Done"
    assert not executor.results


def test_feature_signature_ignores_cosmetic_script_changes():
    from freecad.llm_copilot.agent import _feature_signature
    first = LLMProposal("pad", "pad.Length = 4.0", "", True, plan_step=2)
    second = LLMProposal("pad", "pad.Length = 5.0", "", True, plan_step=2)
    assert (_feature_signature(first, _occ_failure(13))
            == _feature_signature(second, _occ_failure(21)))
    other_step = LLMProposal("hole", "x", "", True, plan_step=3)
    assert (_feature_signature(first, _occ_failure())
            != _feature_signature(other_step, _occ_failure()))


def test_diagnostic_script_is_exempt_from_the_planning_gates():
    """Turn 6 of the climbing-hanger transcript: a pure read-only diagnostic.

    It carried no plan, ledger, or fidelity checklist and was rejected before
    execution, leaving the model to guess at the failure. Diagnosis must run.
    """
    diagnostic = (
        "doc = App.ActiveDocument\n"
        "sk = doc.getObject('PlateSketch')\n"
        "print('closed?', sk.Shape.isClosed())\n")
    client = FakeClient([
        LLMProposal("diagnose the pad", diagnostic, "", True, strategy=""),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "closed? False", "")])
    shown = []
    out = _agent(
        client, executor,
        _settings(auto_approve_loop=True, structured_cad_planning=True,
                  assumption_ledger=True, mandatory_verification=False,
                  final_design_review=False)).send(
            "model a hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    assert out == "Done"
    assert not executor.results, "the diagnostic never ran"
    assert not [text for text in shown if text.startswith("[")]


def test_diagnostic_runs_without_a_replica_feature_checklist():
    """A diagnostic carries no feature checklist; the pre-execution gate must
    not block it. The *completion* fidelity check is unaffected — it still
    requires a resolved checklist before the turn can be called done."""
    diagnostic = "print(App.ActiveDocument.getObject('PlateSketch').Shape)\n"
    client = FakeClient([
        LLMProposal("diagnose", diagnostic, "", True, strategy=""),
        LLMProposal("", "", "Done", False, kind="finish"),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "shape", "")])
    shown = []
    _agent(
        client, executor,
        _settings(auto_approve_loop=True, fidelity_target="replica",
                  mandatory_verification=False, final_design_review=False)).send(
            "replicate hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    # The diagnostic executed rather than being rejected before it ran.
    assert not executor.results, "the diagnostic never ran"
    assert not [text for text in shown if text.startswith("[")]


def test_building_script_still_faces_the_planning_gates():
    """The exemption is narrow: anything that constructs is gated as before."""
    client = FakeClient([
        LLMProposal(
            "pad it", "pad = body.newObject('PartDesign::Pad', 'P')", "",
            True, strategy=""),
        LLMProposal("", "", "Stopped", False, kind="finish"),
    ])
    shown = []
    _agent(
        client, FakeExecutor([]),
        _settings(auto_approve_loop=True)).send(
            "model a hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    assert any(text.startswith("[Part Design required]") for text in shown)


def _assumption(**kw):
    row = {
        "id": "plate_thickness", "name": "Plate thickness", "value": 4.0,
        "unit": "mm", "source": "reference image", "confidence": "medium",
        "consequence": "medium", "status": "unverified", "evidence": "",
        "if_wrong": "plate strength changes",
    }
    row.update(kw)
    return row


def test_first_building_step_is_asked_for_assumptions_and_still_runs():
    """Harness Stage 2: no geometry before the assumption ledger exists."""
    build = "body = doc.addObject('PartDesign::Body', 'B')"
    client = FakeClient([
        LLMProposal("start building", build, "", True),
        LLMProposal(
            "start building", build, "", True,
            assumptions=(_assumption(),)),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 2)
    agent = _agent(
        client, executor,
        _settings(auto_approve_loop=True, assumption_ledger=True))
    out = agent.send(
        "model a hanger", lambda _intent: True, lambda _r, _s, _i, _p: None)
    assert out == "Done"
    asks = [str(call) for call in client.calls
            if "provide an assumption ledger with this step" in str(call)]
    assert asks, "the model must still be asked for a ledger"
    # But the step ran: asking is what matters, not withholding execution.
    assert not executor.results


def test_ledger_first_does_not_fire_when_the_model_leads_with_a_ledger():
    client = FakeClient([
        LLMProposal(
            "start building", "body = doc.addObject('PartDesign::Body', 'B')",
            "", True, assumptions=(_assumption(),)),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown = []
    _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True, assumption_ledger=True)).send(
            "model it", lambda _intent: True, lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    assert not [t for t in shown if t.startswith("[assumptions and plan first]")]


def test_ledger_first_never_blocks_a_diagnostic():
    client = FakeClient([
        LLMProposal("diagnose", "print(doc.Objects)\n", "", True, strategy=""),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown = []
    _agent(
        client, FakeExecutor([ExecResult(True, "[]", "")]),
        _settings(auto_approve_loop=True, assumption_ledger=True,
                  mandatory_verification=False, final_design_review=False)).send(
            "what is in here", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    assert not [t for t in shown if t.startswith("[")]


def test_multi_feature_step_is_advised_not_blocked():
    """Per-feature verification is only real if a step builds one feature."""
    batched = (
        "pad = body.newObject('PartDesign::Pad','PlatePad')\n"
        "pk = body.newObject('PartDesign::Pocket','BoltHole')\n")
    single = "pad = body.newObject('PartDesign::Pad','PlatePad')\n"
    client = FakeClient([
        LLMProposal("pad and pocket", batched, "", True),
        LLMProposal("pad only", single, "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")] * 2)
    agent = _agent(
        client, executor,
        _settings(auto_approve_loop=True, one_feature_per_step=True))
    out = agent.send(
        "model a hanger", lambda _intent: True, lambda _r, _s, _i, _p: None)
    assert out == "Done"
    # Batching hurts diagnosis, not the document, so it advises rather than
    # discarding a script that may be perfectly good.
    advisory = [str(c) for c in client.calls if "2 features" in str(c)]
    assert advisory
    assert not executor.results


def test_one_feature_gate_is_off_by_default():
    batched = (
        "pad = body.newObject('PartDesign::Pad','P')\n"
        "pk = body.newObject('PartDesign::Pocket','H')\n")
    client = FakeClient([
        LLMProposal("pad and pocket", batched, "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown = []
    out = _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True)).send(
            "model it", lambda _intent: True, lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    assert out == "Done"
    assert not [t for t in shown if t.startswith("[one feature per step]")]


def test_placeholder_block_is_rejected_before_execution():
    """The transcript submitted `for i in range(4): pass` where constraints
    belonged, then reasoned as though the sketch were constrained."""
    placeholder = (
        "sk.addGeometry(Part.LineSegment(a, b), False)\n"
        "for i in range(4):\n"
        "    pass\n")
    real = "sk.addGeometry(Part.LineSegment(a, b), False)\n"
    client = FakeClient([
        LLMProposal("sketch the plate", placeholder, "", True),
        LLMProposal("sketch the plate", real, "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")])
    shown = []
    agent = _agent(
        client, executor, _settings(auto_approve_loop=True))
    out = agent.send(
        "model a hanger", lambda _intent: True, lambda _r, _s, _i, _p: None,
        on_tool_result=lambda tool, summary, content: shown.append(content))
    assert out == "Done"
    asks = [t for t in shown if t.startswith("[placeholder code]")]
    assert len(asks) == 1
    assert "for block at line 2" in asks[0]
    assert asks[0] in [
        m["content"] for m in agent.messages if m["role"] == "user"]
    assert not executor.results, "the placeholder script never ran"


def _ledger_row(rid, consequence, confidence="medium"):
    return {
        "id": rid, "name": rid, "value": 4.0, "unit": "mm",
        "source": "hanger3.jpg", "confidence": confidence,
        "consequence": consequence, "if_wrong": "geometry wrong",
        "status": "unverified", "evidence": "",
    }


def test_misordered_ledger_runs_and_is_sorted_by_the_host():
    """Both turns of climbing-hanger-transcript-2 died here.

    A ledger listed in any order was rejected with a complaint that named no
    row, so a complete, working hanger script was discarded twice. Row order is
    presentation: accept it and sort it.
    """
    client = FakeClient([
        LLMProposal(
            "build the hanger",
            "body = doc.addObject('PartDesign::Body','HangerBody')\n", "", True,
            assumptions=(
                _ledger_row("thickness", "medium"),
                _ledger_row("plate_h", "high"),
                _ledger_row("bolt_d", "low"))),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "HangerBody", "")])
    shown = []
    agent = _agent(
        client, executor,
        _settings(auto_approve_loop=True, assumption_ledger=True,
                  mandatory_verification=False, final_design_review=False))
    stored = []
    original = cad_workflow.sort_assumptions

    def capture(rows):
        result = original(rows)
        stored[:] = result
        return result

    cad_workflow.sort_assumptions = capture
    try:
        out = agent.send(
            "model a hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    finally:
        cad_workflow.sort_assumptions = original
    assert out == "Done"
    assert not [t for t in shown if t.startswith("[assumption ledger required]")]
    assert not executor.results, "the script never ran"
    # Stored most-severe-first regardless of the order the model chose.
    assert [row["id"] for row in stored] == ["plate_h", "thickness", "bolt_d"]


def test_identical_gate_rejection_escalates_instead_of_looping():
    """A gate repeating itself verbatim is a deadlock, not correction."""
    bad = LLMProposal("try part", "pass", "", True, strategy="part")
    client = FakeClient([
        bad, bad,
        LLMProposal("native", "pass", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown = []
    agent = _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True))
    out = agent.send(
        "make it", lambda _intent: True, lambda _r, _s, _i, _p: None,
        on_tool_result=lambda tool, summary, content: shown.append(content))
    assert out == "Done"
    escalated = [t for t in shown if "[identical rejection repeated]" in t]
    assert len(escalated) == 1
    assert "rejection 2 with exactly the same text" in escalated[0]
    # Still single-sourced: the user sees exactly what the model was told.
    assert escalated[0] in [
        m["content"] for m in agent.messages if m["role"] == "user"]


def test_a_different_rejection_resets_the_repeat_counter():
    placeholder = "for i in range(2):\n    pass\n"
    client = FakeClient([
        LLMProposal("try part", "pass", "", True, strategy="part"),
        LLMProposal("inert", placeholder, "", True),
        LLMProposal("try part", "pass", "", True, strategy="part"),
        LLMProposal("native", "pass", "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown = []
    _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True)).send(
            "make it", lambda _intent: True, lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    assert not [t for t in shown if "[identical rejection repeated]" in t]


def test_one_blocker_and_two_advisories_arrive_in_a_single_message():
    """Objections surfaced one per round cost a script each time."""
    script = (
        "pad = body.newObject('PartDesign::Pad','P')\n"
        "pk = body.newObject('PartDesign::Pocket','H')\n")
    client = FakeClient([
        # Blocks on strategy; also trips plan and one-feature advisories.
        LLMProposal("do everything", script, "", True, strategy="part"),
        LLMProposal(
            "pad only", "pad = body.newObject('PartDesign::Pad','P')\n", "",
            True, strategy="part_design", stage="additive",
            plan=("Pad the plate",), plan_step=1,
            success_criteria=("one solid",)),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    shown = []
    _agent(
        client, FakeExecutor([ExecResult(True, "", "")]),
        _settings(auto_approve_loop=True, structured_cad_planning=True,
                  one_feature_per_step=True)).send(
            "model a hanger", lambda _intent: True,
            lambda _r, _s, _i, _p: None,
            on_tool_result=lambda tool, summary, content: shown.append(content))
    rejections = [t for t in shown if t.startswith("[")]
    assert len(rejections) == 1, "every objection belongs in one message"
    text = rejections[0]
    assert "[Part Design required]" in text          # the blocker
    assert "also advisory" in text
    assert "ordered feature-level plan" in text      # advisory 1
    assert "2 features" in text                      # advisory 2


def test_only_three_rules_block_a_step():
    """The blocking set: non-Part-Design, placeholder code, blocking assumption."""
    blockers = (
        ("part design", LLMProposal("x", "pass", "", True, strategy="part")),
        ("placeholder", LLMProposal(
            "x", "for i in range(2):\n    pass\n", "", True)),
    )
    for label, bad in blockers:
        client = FakeClient([bad, LLMProposal("ok", "pass", "", True),
                             LLMProposal("", "", "Done", False, kind="finish")])
        executor = FakeExecutor([ExecResult(True, "", "")])
        _agent(client, executor, _settings(auto_approve_loop=True)).send(
            "make it", lambda _intent: True, lambda _r, _s, _i, _p: None)
        assert not executor.results, f"{label} must block execution"


def test_advisory_only_script_runs_and_carries_its_advisories():
    client = FakeClient([
        LLMProposal("no plan, batched", (
            "pad = body.newObject('PartDesign::Pad','P')\n"
            "pk = body.newObject('PartDesign::Pocket','H')\n"), "", True),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")])
    _agent(
        client, executor,
        _settings(auto_approve_loop=True, structured_cad_planning=True,
                  one_feature_per_step=True)).send(
            "model it", lambda _intent: True, lambda _r, _s, _i, _p: None)
    assert not executor.results, "advisories must not withhold execution"
    tail = str(client.calls[-1])
    assert "[workflow advisories]" in tail
    assert "2 features" in tail


def test_blocking_assumption_still_stops_the_step():
    """A low-confidence, high-consequence value would bake into every feature."""
    risky = _assumption(confidence="low", consequence="high")
    options = (
        {"id": "a", "label": "68 mm", "description": "tall"},
        {"id": "b", "label": "50 mm", "description": "short"},
    )
    client = FakeClient([
        LLMProposal("build", "pass", "", True, assumptions=(risky,)),
        LLMProposal("", "", "", False, kind="question",
                    question="How tall?", options=options),
        LLMProposal("build", "pass", "", True, assumptions=(
            _assumption(confidence="high", consequence="high",
                        status="user_confirmed", evidence="user chose 68"),)),
        LLMProposal("", "", "Done", False, kind="finish"),
    ])
    executor = FakeExecutor([ExecResult(True, "", "")])
    out = _agent(
        client, executor,
        _settings(auto_approve_loop=True, assumption_ledger=True)).send(
            "make it", lambda _intent: True, lambda _r, _s, _i, _p: None,
            on_question=lambda _proposal: ["a"])
    assert out == "Done"
    assert not executor.results
    assert any("[assumption clarification required]" in str(c)
               for c in client.calls)
