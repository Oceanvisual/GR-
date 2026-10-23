"""Golden annotation of an eval document and the dataset entries that point to it.

A golden process is given by the fragments that hold it (`roots`, by their labels; each root
brings its subtree), so matching does not depend on how the model names the process. Only
`required` processes count for recall; the optional ones are acceptable answers that do not
count against precision (control, appeals, the same procedure through another channel).
Operations are the key actions, each tied to the fragments that state it.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

EVAL_DIR = Path(__file__).resolve().parent


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldenOperation(_Strict):
    name: str = Field(min_length=1)
    labels: list[str] = Field(min_length=1)  # fragments where the action is stated


class GoldenDeadline(_Strict):
    value: int | None = Field(ge=0)  # None for «не позднее следующего рабочего дня»
    unit: str
    label: str
    what: str = ""


class GoldenProcess(_Strict):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = []
    required: bool = True
    roots: list[str] = Field(min_length=1)
    operations: list[GoldenOperation] = []
    deadlines: list[GoldenDeadline] = []
    note: str = ""


class Golden(_Strict):
    document: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition: str  # what counts as a process in this document
    annotated_by: str
    verified_by: str | None = None  # the expert who checked it; None until then
    processes: list[GoldenProcess]

    def required(self) -> list[GoldenProcess]:
        return [p for p in self.processes if p.required]


class Case(_Strict):
    """A line of dataset.jsonl: one document of the eval set."""

    id: str = Field(min_length=1)
    path: str  # relative to the repository root
    golden: str  # relative to evals/
    tags: list[str] = []

    def load_golden(self) -> Golden:
        return Golden.model_validate_json((EVAL_DIR / self.golden).read_text("utf-8"))


def load_cases(path: Path = EVAL_DIR / "dataset.jsonl") -> list[Case]:
    cases = [
        Case.model_validate(json.loads(line))
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate case ids in {path.name}: {ids}")
    return cases
