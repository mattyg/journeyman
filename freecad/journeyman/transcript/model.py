"""Durable conversation aggregate and its model-facing projection."""


_INSPECTION_HEADERS = (
    "[inspection result]\n", "[verify-stage inspection result]\n")


def _is_inspection(message):
    if message.get("ephemeral") == "inspection":
        return True
    content = message.get("content")
    return isinstance(content, str) and content.startswith(_INSPECTION_HEADERS)


def _live_inspection_index(messages):
    from ..workflow import feedback as turn_protocol
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_inspection(message):
            return index
        content = message.get("content")
        if (isinstance(content, str)
                and turn_protocol.is_document_changing_result(content)):
            return None
    return None


def _last_document_change_index(messages):
    from ..workflow import feedback as turn_protocol
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].get("content")
        if (isinstance(content, str)
                and turn_protocol.is_document_changing_result(content)):
            return index
    return None


def model_history(messages):
    """Project durable events into compact provider-facing messages."""
    from ..workflow import feedback as turn_protocol
    live = _live_inspection_index(messages)
    changed_at = _last_document_change_index(messages)
    compact = []
    for index, message in enumerate(messages):
        if (message.get("ephemeral") == "render" and changed_at is not None
                and index < changed_at):
            continue
        item = dict(message)
        item.pop("ephemeral", None)
        query = item.pop("inspection_query", "")
        if _is_inspection(message) and index != live:
            item["content"] = turn_protocol.superseded_inspection(
                query or "(query not recorded)",
                verify_stage=str(message.get("content", "")).startswith(
                    "[verify-stage"))
            compact.append(item)
            continue
        content = item.get("content")
        if isinstance(content, str):
            if content.startswith("[document snapshot]\n"):
                marker = "\n\n[request]\n"
                if marker in content:
                    content = "[request]\n" + content.split(marker, 1)[1]
            for marker in ("\n[new snapshot]\n", "\n[design ledger]\n"):
                if marker in content:
                    content = content.split(marker, 1)[0].rstrip() + "\n"
            if ("\n(script)\n" in content and index + 1 < len(messages)
                    and str(messages[index + 1].get("content", "")).startswith(
                        "[executed OK]")):
                content = content.split("\n(script)\n", 1)[0].replace(
                    "(intent)", "(executed intent)", 1)
            item["content"] = content
        compact.append(item)
    return compact


class Transcript:
    """One owner for durable messages, UI entries, and derived projections."""

    def __init__(self, messages=None, entries=None):
        self.messages = list(messages or ())
        self.entries = list(entries or ())

    def model_messages(self):
        return model_history(self.messages)

    def replace_messages(self, messages):
        self.messages = list(messages or ())
