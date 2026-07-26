"""Non-Qt lifecycle state for one document-bound conversation."""


class DocumentSession(dict):
    """Own an agent, transcript, and asynchronous UI lifecycle state.

    Mapping compatibility keeps Qt slots concise during migration, while all
    construction and transcript invariants live here and are directly testable.
    """

    def __init__(self, agent, document=None, entries=None,
                 persistence_loaded=False):
        transcript_entries = (agent.transcript.entries if entries is None
                              else entries)
        agent.transcript.entries = transcript_entries
        super().__init__({
            "agent": agent, "entries": transcript_entries,
            "reason_seq": 0, "step_seq": 0, "context_seq": 0,
            "document": document, "scroll": 0,
            "persistence_loaded": persistence_loaded,
            "busy": False, "cancel_event": None,
            "status_entry": None, "elapsed": 0,
            "waiting_for_question": False,
            "pending_script_tool": None, "pending_info_tool": None,
        })

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "entries" and "agent" in self:
            self["agent"].transcript.entries = value

    @property
    def agent(self):
        return self["agent"]

    @property
    def transcript(self):
        return self.agent.transcript

    def persist(self, save):
        """Persist through an injected storage function when enabled."""
        if self.get("document") is None:
            return False
        if not self.agent.settings.persist_chat_history:
            return False
        save(self["document"], self.transcript.messages,
             self.transcript.entries)
        return True

    def clear_conversation(self):
        self.transcript.messages.clear()
        self.transcript.entries.clear()
        self["reason_seq"] = 0
        self["step_seq"] = 0
        self["context_seq"] = 0

