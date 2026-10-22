"""LLM-as-judge for S6 (T066): is the choice of technology justified, is the initiative concrete.

The judge reads one initiative at a time (prompts/judge_initiative.md) and answers with a
`JudgeVerdict`. Verdicts are cached in `judge_cache.json` (in git, like the baseline) by the
hash of the model, the prompt version and the initiative, so a rescore or a repeated run costs
nothing and gives the same numbers; only a new initiative or a new prompt calls the model.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from evals.metrics_opportunities import PredInitiative, judged
from gr.domain.enums import StageName
from gr.llm.base import StructuredLLM

PROMPT = Path(__file__).parent / "prompts" / "judge_initiative.md"
CACHE = Path(__file__).parent / "judge_cache.json"
MAX_TOKENS = 1000
_HEADER_RE = re.compile(r"\A---\nversion:\s*(\S+)\n---\n")
_SECTION_RE = re.compile(r"^## (system|task)\n", re.MULTILINE)


class JudgeVerdict(BaseModel):
    grounded: bool
    grounded_reason: str = Field(max_length=600)
    concrete: bool
    concrete_reason: str = Field(max_length=600)


def load_judge_prompt(path: Path = PROMPT) -> tuple[str, str, str]:
    """(version, system, task) of the judge prompt."""
    text = path.read_text("utf-8")
    header = _HEADER_RE.match(text)
    if header is None:
        raise ValueError(f"{path.name}: no version header")
    parts = _SECTION_RE.split(text[header.end() :])
    sections = dict(zip(parts[1::2], (p.strip() for p in parts[2::2]), strict=True))
    if set(sections) != {"system", "task"}:
        raise ValueError(f"{path.name} needs exactly '## system' and '## task'")
    return f"judge_initiative@{header.group(1)}", sections["system"], sections["task"]


def _payload(initiative: PredInitiative) -> str:
    data = initiative.model_dump(
        mode="json",
        include={"title", "description", "architecture_idea", "tech_rationale",
                 "solution_category", "min_category", "patterns", "alternatives"},
    )  # fmt: skip
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class Judge:
    def __init__(self, llm: StructuredLLM, model: str, cache: Path = CACHE) -> None:
        self.llm, self.model, self.cache_path = llm, model, cache
        self.version, self.system, self.task = load_judge_prompt()
        self.cache: dict[str, dict[str, object]] = (
            json.loads(cache.read_text("utf-8")) if cache.exists() else {}
        )
        self.calls = 0

    def key(self, initiative: PredInitiative) -> str:
        source = "\n".join((self.model, self.version, _payload(initiative)))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    async def verdict(self, initiative: PredInitiative) -> JudgeVerdict:
        key = self.key(initiative)
        if key in self.cache:
            return JudgeVerdict.model_validate(self.cache[key])
        user = f"{self.task}\n\n<initiative>\n{_payload(initiative)}\n</initiative>"
        result = await self.llm.generate(
            schema=JudgeVerdict,
            system=self.system,
            user=user,
            stage=StageName.OPPORTUNITIES,  # the stage judged: its model is the analysis one
            max_tokens=MAX_TOKENS,
            run_id="eval-judge",
        )
        self.calls += 1
        self.cache[key] = result.value.model_dump(mode="json")
        return result.value

    async def score(self, initiatives: Sequence[PredInitiative]) -> dict[str, float | None]:
        verdicts = [await self.verdict(i) for i in initiatives]
        return judged((v.grounded, v.concrete) for v in verdicts)

    def save(self) -> None:
        ordered = dict(sorted(self.cache.items()))
        self.cache_path.write_text(
            json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", "utf-8"
        )
