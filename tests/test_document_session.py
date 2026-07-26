from types import SimpleNamespace

from freecad.journeyman.document_session import DocumentSession
from freecad.journeyman.transcript import Transcript


def _agent(persist=True):
    return SimpleNamespace(
        transcript=Transcript([{"role": "user", "content": "x"}],
                              [{"kind": "user"}]),
        settings=SimpleNamespace(persist_chat_history=persist))


def test_session_keeps_entries_owned_by_transcript():
    agent = _agent()
    session = DocumentSession(agent, object())
    assert session["entries"] is agent.transcript.entries
    replacement = [{"kind": "text"}]
    session["entries"] = replacement
    assert agent.transcript.entries is replacement


def test_session_persists_whole_transcript_when_enabled():
    agent = _agent()
    document = object()
    session = DocumentSession(agent, document)
    calls = []
    assert session.persist(lambda *args: calls.append(args)) is True
    assert calls == [(document, agent.transcript.messages,
                      agent.transcript.entries)]


def test_session_clear_resets_conversation_sequences():
    session = DocumentSession(_agent(), object())
    session.update(reason_seq=3, step_seq=4, context_seq=5)
    session.clear_conversation()
    assert session.transcript.messages == []
    assert session.transcript.entries == []
    assert (session["reason_seq"], session["step_seq"],
            session["context_seq"]) == (0, 0, 0)
