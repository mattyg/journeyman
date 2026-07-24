# freecad/llm_copilot/agent.py

# Max times per request we re-prompt a model that replied with a script in prose
# instead of calling the tool, before giving up and accepting the text.
_MAX_NUDGES = 2

# Markers that a plain-text reply is really an attempt to run code (so we should
# nudge the model to use the tool rather than accept it as a finished answer).
_ACTION_MARKERS = ("(script)", "(intent)", "doc.", "App.ActiveDocument",
                   "addObject", "```")


def _looks_like_action(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m.lower() in low for m in _ACTION_MARKERS)


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
        nudges = 0
        while True:
            proposal = self.client.complete(self.messages, self.settings)
            reasoning = getattr(proposal, "reasoning", "")
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if not proposal.is_tool_call:
                # Some providers/models describe a script in prose instead of
                # calling the tool. If the "final" text looks like it's trying to
                # act (contains a script/intent block or code), nudge it back to
                # the tool rather than accepting it as a finished answer.
                if nudges < _MAX_NUDGES and _looks_like_action(proposal.text):
                    nudges += 1
                    self.messages.append(
                        {"role": "assistant", "content": proposal.text})
                    self.messages.append({
                        "role": "user",
                        "content": ("Do not describe scripts in plain text. To run "
                                    "that code, call the run_freecad_script tool "
                                    "with `intent` and `script`."),
                    })
                    continue
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
                output = getattr(result, "output", "") or ""
                out_block = f"[script output]\n{output}\n" if output.strip() else ""
                self.messages.append({
                    "role": "user",
                    "content": f"[executed OK]\n{out_block}[new snapshot]\n{new_snap}",
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
            output = getattr(result, "output", "") or ""
            out_block = f"[script output]\n{output}\n" if output.strip() else ""
            self.messages.append({
                "role": "user",
                "content": (f"[script failed]\n{out_block}{result.error}\n"
                            "Fix the script and call the run_freecad_script tool "
                            "again — do not reply in plain text."),
            })
