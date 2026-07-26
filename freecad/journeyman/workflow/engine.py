"""Stateful workflow policy for one agent turn.

This module owns the ordering-sensitive part of CAD proposal validation.  The
agent supplies proposals and executes accepted scripts; it no longer needs to
know how individual planning, assumption, fidelity, and script-shape rules
mutate the turn ledger.
"""

from . import feedback as turn_protocol
from . import policy as cad_workflow


class TurnState:
    """Mutable state and ledger for one model/agent exchange."""

    def __init__(self):
        self.executed_steps = 0
        self.retries = 0
        self.planning_retries = 0
        self.completion_retries = 0
        self.question_retries = 0
        self.assumption_retries = 0
        self.assumptions_accepted = False
        self.assumption_clarification = False
        self.assumption_questions = 0
        self.fidelity_retries = 0
        self.fidelity_clarification = False
        self.fidelity_questions = 0
        self.part_design_retries = 0
        self.feature_signature = None
        self.feature_failures = 0
        self.ledger_first_requested = False
        self.multi_feature_retries = 0
        self.noop_retries = 0
        self.last_rejection = None
        self.repeated_rejections = 0
        self.best_state = None
        self.best_solids = 0
        self.ledger = {
            "strategy": "", "stage": "analyze", "plan": (),
            "success_criteria": (), "completed_stages": set(),
            "completed_steps": 0, "warnings": (),
        }


class WorkflowEngine:
    """Apply workflow policy while keeping its state transitions local."""

    def __init__(self, settings, state=None):
        self.settings = settings
        self.state = state or TurnState()

    @property
    def ledger(self):
        return self.state.ledger

    def pre_execution_checks(self, proposal, diagnostic):
        """Return ``(blocking, advisories, abort)`` for a script proposal."""
        turn = self.state
        ledger = turn.ledger
        blocking, advisories = [], []
        limit = max(1, self.settings.self_correction_attempts)

        noop_issues = cad_workflow.noop_block_issues(proposal.script)
        if noop_issues:
            turn.noop_retries += 1
            if turn.noop_retries > limit:
                return (), (), (
                    "I couldn't get a script without placeholder blocks: "
                    + "; ".join(noop_issues) + ".")
            blocking.append(turn_protocol.placeholder_code(noop_issues))
        else:
            turn.noop_retries = 0

        if not diagnostic and proposal.strategy != "part_design":
            turn.part_design_retries += 1
            if turn.part_design_retries > limit:
                return (), (), "I couldn't produce a native Part Design proposal."
            blocking.append(turn_protocol.part_design_required())

        if self.settings.structured_cad_planning and not diagnostic:
            issues = cad_workflow.proposal_issues(proposal)
            if issues:
                advisories.extend(issues)
            else:
                ledger.update({
                    "strategy": proposal.strategy, "stage": proposal.stage,
                    "plan": proposal.plan, "plan_step": proposal.plan_step,
                    "success_criteria": proposal.success_criteria,
                })
        else:
            for key, value in (
                    ("strategy", proposal.strategy), ("stage", proposal.stage),
                    ("plan", proposal.plan),
                    ("success_criteria", proposal.success_criteria)):
                if value:
                    ledger[key] = value

        if self.settings.assumption_ledger and not diagnostic:
            self._assumption_checks(proposal, blocking, advisories)
        elif self.settings.assumption_ledger and proposal.assumptions is not None:
            self._assumption_update_checks(proposal, advisories)

        if self.settings.fidelity_target == "replica" and not diagnostic:
            features, issues = cad_workflow.fidelity_feature_issues(
                ledger.get("observed_features"), proposal.observed_features)
            if not issues:
                if (any(row["status"] == "user_approved_omission"
                        for row in features) and turn.fidelity_questions == 0):
                    turn.fidelity_clarification = True
                    issues.append("ask the user before omitting an observed feature")
                if any(row["status"] == "blocked" for row in features):
                    issues.append(
                        "change construction strategy to implement blocked "
                        "features; difficulty is not permission to omit them")
            advisories.extend(issues)
            if features:
                ledger["observed_features"] = features
                turn.fidelity_clarification = False

        if self.settings.one_feature_per_step and not diagnostic:
            advisories.extend(cad_workflow.multi_feature_issues(proposal.script))
        return tuple(blocking), tuple(advisories), None

    def _assumption_checks(self, proposal, blocking, advisories):
        turn = self.state
        if turn.assumptions_accepted:
            if proposal.assumptions is not None:
                self._assumption_update_checks(proposal, advisories)
            return
        if (not turn.ledger_first_requested
                and turn.ledger.get("assumptions") is None
                and proposal.assumptions is None):
            turn.ledger_first_requested = True
            advisories.append(
                "provide an assumption ledger with this step: every numeric "
                "value not given in the request, with source, confidence, "
                "consequence and status")
            return
        issues = cad_workflow.assumption_ledger_missing(proposal, turn)
        if not issues and turn.ledger.get("assumptions") is not None:
            advisories.extend(cad_workflow.relabelled_assumptions(
                turn.ledger["assumptions"], proposal.assumptions))
            merged, merge_issues = cad_workflow.merge_assumptions(
                turn.ledger["assumptions"], proposal.assumptions)
            issues.extend(merge_issues)
        else:
            merged = cad_workflow.sort_assumptions(
                dict(row) for row in (proposal.assumptions or ()))
        advisories.extend(issues)
        turn.ledger["assumptions"] = merged
        blocking_rows = cad_workflow.blocking_assumptions(merged)
        if blocking_rows:
            turn.assumption_clarification = True
            blocking.append(turn_protocol.assumption_clarification_required(
                [row["id"] for row in blocking_rows]))
            return
        if turn.assumption_clarification and turn.assumption_questions == 0:
            blocking.append(turn_protocol.assumption_clarification_required())
            return
        turn.assumptions_accepted = True
        turn.assumption_clarification = False
        turn.assumption_retries = 0

    def _assumption_update_checks(self, proposal, advisories):
        turn = self.state
        pending = type("_Pending", (), {"assumptions_accepted": False})()
        issues = cad_workflow.assumption_ledger_missing(proposal, pending)
        advisories.extend(cad_workflow.relabelled_assumptions(
            turn.ledger.get("assumptions", ()), proposal.assumptions))
        merged, merge_issues = cad_workflow.merge_assumptions(
            turn.ledger.get("assumptions", ()), proposal.assumptions)
        issues.extend(merge_issues)
        advisories.extend(issues)
        turn.ledger["assumptions"] = merged
