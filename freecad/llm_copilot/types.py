# freecad/llm_copilot/types.py
from dataclasses import dataclass

@dataclass
class ExecResult:
    ok: bool
    output: str
    error: str
