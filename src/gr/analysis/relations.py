"""Symmetric initiative relations and alternative components (FR-048, T039)."""

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gr.domain.enums import InitiativeRelation
from gr.domain.initiative import Initiative, Relation


class ExpertRelation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    left_id: str = Field(min_length=1)
    right_id: str = Field(min_length=1)
    relation: InitiativeRelation

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.left_id == self.right_id:
            raise ValueError("an initiative cannot relate to itself")
        return self


def infer_relations(
    initiatives: Sequence[Initiative],
    *,
    overrides: Sequence[ExpertRelation] = (),
) -> list[Initiative]:
    by_id = {i.id: i for i in initiatives}
    if len(by_id) != len(initiatives):
        raise ValueError("initiative ids must be unique")
    expert: dict[tuple[str, ...], InitiativeRelation] = {}
    for initiative in initiatives:
        for relation in initiative.relations:
            if relation.source != "expert":
                continue
            if relation.initiative_id not in by_id or relation.initiative_id == initiative.id:
                raise ValueError("invalid stored expert relation")
            pair = tuple(sorted((initiative.id, relation.initiative_id)))
            if pair in expert and expert[pair] != relation.relation:
                raise ValueError("conflicting stored expert relations")
            expert[pair] = relation.relation
    updates: dict[tuple[str, ...], InitiativeRelation] = {}
    for item in overrides:
        if item.left_id not in by_id or item.right_id not in by_id:
            raise ValueError("expert relation refers to an unknown initiative")
        pair = tuple(sorted((item.left_id, item.right_id)))
        if pair in updates and updates[pair] != item.relation:
            raise ValueError("conflicting expert relations")
        updates[pair] = item.relation
    expert.update(updates)
    related: dict[str, list[Relation]] = {key: [] for key in by_id}
    ordered = sorted(by_id.values(), key=lambda i: i.id)
    for n, left in enumerate(ordered):
        for right in ordered[n + 1 :]:
            pair = (left.id, right.id)
            source: Literal["auto", "expert"] = "auto"
            if pair in expert:
                kind, source = expert[pair], "expert"
            elif set(left.operation_ids) & set(right.operation_ids):
                kind = (
                    InitiativeRelation.ALTERNATIVE
                    if set(left.finding_ids) & set(right.finding_ids)
                    else InitiativeRelation.COMPLEMENTARY
                )
            else:
                continue
            related[left.id].append(Relation(initiative_id=right.id, relation=kind, source=source))
            related[right.id].append(Relation(initiative_id=left.id, relation=kind, source=source))
    return [i.model_copy(update={"relations": related[i.id]}) for i in initiatives]


def alternative_components(initiatives: Sequence[Initiative]) -> list[list[str]]:
    """Deterministic connected components, including isolated initiatives as singletons."""
    graph: dict[str, set[str]] = {i.id: set() for i in initiatives}
    if len(graph) != len(initiatives):
        raise ValueError("initiative ids must be unique")
    for item in initiatives:
        for relation in item.relations:
            other = relation.initiative_id
            if other not in graph or other == item.id:
                raise ValueError("invalid initiative relation")
            if relation.relation == InitiativeRelation.ALTERNATIVE:
                graph[item.id].add(other)
                graph[other].add(item.id)
    seen: set[str] = set()
    components: list[list[str]] = []
    for key in sorted(graph):
        if key in seen:
            continue
        stack = [key]
        component: set[str] = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(graph[node] - component)
        seen |= component
        components.append(sorted(component))
    return components
