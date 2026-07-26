from freecad.journeyman.context_usage import estimate_tokens, format_usage


def test_usage_increases_with_conversation():
    base = estimate_tokens([], "system", [{"name": "tool"}])
    used = estimate_tokens(
        [{"role": "user", "content": "make a cube " * 100}],
        "system", [{"name": "tool"}])
    assert used > base


def test_usage_measures_the_wire_not_the_stored_transcript():
    # A superseded inspection stays in the transcript but is not sent, so the
    # estimate must not keep charging for it.
    huge = "X" * 20000
    messages = [
        {"role": "user", "content": "[inspection result]\n" + huge,
         "ephemeral": "inspection", "inspection_query": "first"},
        {"role": "user", "content": "[inspection result]\nsmall",
         "ephemeral": "inspection", "inspection_query": "second"},
    ]
    assert estimate_tokens(messages, "system", []) < len(huge) / 4


def test_usage_label_reports_estimate_and_message_count():
    label = format_usage(
        [{"role": "user", "content": "hello"}], "system", [])
    assert label.startswith("Context: ~")
    assert "tokens" in label
    assert "1 message" in label
