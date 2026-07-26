from freecad.journeyman.transcript import Transcript


def test_transcript_owns_durable_and_ui_records():
    transcript = Transcript([{"role": "user", "content": "hello"}],
                            [{"kind": "user", "text": "hello"}])
    assert transcript.messages[0]["content"] == "hello"
    assert transcript.entries[0]["kind"] == "user"


def test_model_messages_are_a_projection_not_a_mutation():
    transcript = Transcript([{
        "role": "user", "content": "[inspection result]\nold",
        "ephemeral": "inspection", "inspection_query": "Pad.Length",
    }])
    projected = transcript.model_messages()
    assert "ephemeral" not in projected[0]
    assert transcript.messages[0]["ephemeral"] == "inspection"
