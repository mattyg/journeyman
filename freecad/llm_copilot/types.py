# freecad/llm_copilot/types.py
from dataclasses import dataclass

@dataclass
class ExecResult:
    ok: bool
    output: str
    error: str
    validation_ok: bool = True
    validation: str = ""
    rolled_back: bool = False
    stderr: str = ""
    console_warnings: str = ""
    console_errors: str = ""
