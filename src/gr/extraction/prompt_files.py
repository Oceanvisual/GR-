"""Stage prompts from `extraction/prompts/<stage>.md` with their versions (contracts/llm.md).

A prompt file starts with a `version:` header, then `## system` and `## task` sections. The
shared rules (`_rules.md`) open every system prompt; the reported version names both files,
so a change to either bumps the version that goes into the run config and the cache key.
"""

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

_HEADER_RE = re.compile(r"\A---\nversion:\s*(\S+)\n---\n", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (system|task)\n", re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    stage: str
    version: str  # "discover@1.0.0+rules@1.0.0"
    system: str
    task: str


def _read(name: str) -> tuple[str, str]:
    text = resources.files("gr.extraction.prompts").joinpath(f"{name}.md").read_text("utf-8")
    match = _HEADER_RE.match(text)
    if match is None:
        raise ValueError(f"prompt {name}.md has no version header")
    return match.group(1), text[match.end() :]


@cache
def load_prompt(stage: str) -> Prompt:
    rules_version, rules = _read("_rules")
    version, body = _read(stage)
    parts = _SECTION_RE.split(body)
    sections = dict(zip(parts[1::2], (p.strip() for p in parts[2::2]), strict=True))
    if set(sections) != {"system", "task"}:
        raise ValueError(f"prompt {stage}.md needs exactly '## system' and '## task'")
    return Prompt(
        stage=stage,
        version=f"{stage}@{version}+rules@{rules_version}",
        system=f"{rules.strip()}\n\n{sections['system']}",
        task=sections["task"],
    )
