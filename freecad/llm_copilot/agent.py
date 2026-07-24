# freecad/llm_copilot/agent.py
class Agent:
    def __init__(self, client, inspector, executor, app, settings):
        self.client = client
        self.inspector = inspector
        self.executor = executor
        self.app = app
        self.settings = settings
        self.messages = []

    def send(self, user_message, on_intent, on_result, on_reasoning=None) -> str:
        snap = self.inspector(self.app)
        self.messages.append({
            "role": "user",
            "content": f"[document snapshot]\n{snap}\n\n[request]\n{user_message}",
        })
        executed_steps = 0
        retries = 0
        while True:
            proposal = self.client.complete(self.messages, self.settings)
            reasoning = getattr(proposal, "reasoning", "")
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if not proposal.is_tool_call:
                self.messages.append({"role": "assistant", "content": proposal.text})
                return proposal.text

            # record the assistant's tool intent in history
            self.messages.append(
                {"role": "assistant",
                 "content": f"(intent) {proposal.intent}\n(script)\n{proposal.script}"})

            gate_needed = (self.settings.confirm_before_running
                           and not self.settings.auto_approve_loop)
            if gate_needed and not on_intent(proposal.intent):
                note = "Cancelled before running."
                self.messages.append({"role": "user", "content": note})
                return note

            result = self.executor.run(self.app, proposal.script)
            new_snap = self.inspector(self.app)
            on_result(result, new_snap)

            if result.ok:
                executed_steps += 1
                retries = 0
                self.messages.append({
                    "role": "user",
                    "content": f"[executed OK]\n[new snapshot]\n{new_snap}",
                })
                if executed_steps >= self.settings.max_auto_approved_steps:
                    summary = ("Paused after reaching the step limit "
                               f"({self.settings.max_auto_approved_steps}). "
                               "Tell me to continue if this looks right.")
                    self.messages.append({"role": "assistant", "content": summary})
                    return summary
                continue

            # error path
            retries += 1
            if retries >= self.settings.self_correction_attempts:
                summary = ("I couldn't complete this after "
                           f"{retries} attempts. Last error:\n{result.error}")
                self.messages.append({"role": "assistant", "content": summary})
                return summary
            self.messages.append({
                "role": "user",
                "content": f"[script failed]\n{result.error}\nPlease fix and try again.",
            })
