"""Priority groups: the six rules of the spec and their order (FR-063, FR-064, T048)."""

import random
from decimal import Decimal

import pytest

from gr.analysis.grouping import assign_group, build_priorities
from gr.analysis.scoring import ScoreCard, load_priority_config
from gr.domain.enums import KnowledgeLayer, LegalGate, PriorityGroup
from gr.domain.priority import CriterionScore

CONFIG = load_priority_config()  # cutoff 35, ease ≥ 0.6, effect ≥ 0.4
QW, ST, CX, NR = (
    PriorityGroup.QUICK_WIN,
    PriorityGroup.STRATEGIC,
    PriorityGroup.COMPLEX,
    PriorityGroup.NOT_RECOMMENDED,
)


def card(
    *,
    score: str = "60",
    ease: str = "0.5",
    effect: str = "0.3",
    money: str = "1500000",
    gate: LegalGate = LegalGate.ALLOWED,
    key: str = "ini_1",
) -> ScoreCard:
    normalised = Decimal(effect)
    return ScoreCard(
        initiative_id=key,
        criteria={
            "effect": CriterionScore(
                raw=Decimal(money),
                normalized=normalised,
                weight=Decimal(30),
                contribution=Decimal(0),
            )
        },
        score=Decimal(score),
        ease=Decimal(ease),
        effect=Decimal(money),
        gate=gate,
    )


@pytest.mark.parametrize(
    ("given", "group", "rule"),
    [
        # 1. a blocked gate wins over anything, even a perfect score and a quick win
        (card(gate=LegalGate.BLOCKED, score="99", ease="0.9", effect="1"), NR, 1),
        # 2. no positive solo effect, or a score under the cutoff
        (card(money="0", score="99", ease="0.9"), NR, 2),
        (card(money="-250000", score="80"), NR, 2),
        (card(score="34.9", ease="0.9", effect="0.9"), NR, 2),
        (card(gate=LegalGate.REQUIRES_REGULATORY_CHANGE, money="-1"), NR, 2),  # 2 before 3
        # 3. the act has to change: complex, even when easy and big
        (card(gate=LegalGate.REQUIRES_REGULATORY_CHANGE, ease="0.9", effect="0.9"), CX, 3),
        # 4. easy wins before big
        (card(ease="0.6", effect="0.9"), QW, 4),
        (card(gate=LegalGate.ALLOWED_WITH_HITL, ease="0.75"), QW, 4),
        # 5. big but not easy
        (card(ease="0.59", effect="0.4"), ST, 5),
        # 6. neither
        (card(ease="0.59", effect="0.39"), CX, 6),
        (card(score="35", ease="0.2", effect="0.1"), CX, 6),  # the cutoff itself passes
    ],
)
def test_each_rule_and_their_order(given: ScoreCard, group: PriorityGroup, rule: int) -> None:
    found, number, reason = assign_group(given, CONFIG)
    assert (found, number) == (group, rule)
    assert reason


def test_reasons_carry_the_numbers_they_were_decided_on() -> None:
    assert assign_group(card(score="31.4"), CONFIG)[2] == "Балл 31,4 ниже порога отсечения 35."
    assert assign_group(card(ease="0.7333"), CONFIG)[2] == "Лёгкость внедрения 0,73 ≥ порога 0,6."
    _, _, reason = assign_group(card(ease="0.5", effect="0.2"), CONFIG)
    assert reason == (
        "Высокая сложность при умеренном эффекте: лёгкость 0,5 < 0,6, эффект 0,2 < 0,4."
    )


def test_every_card_gets_exactly_one_group_consistent_with_the_rules() -> None:
    rng = random.Random(11)
    gates = list(LegalGate)
    seen: set[int] = set()
    for n in range(300):
        given = card(
            score=str(rng.randint(0, 1000) / 10),
            ease=str(rng.randint(0, 100) / 100),
            effect=str(rng.randint(0, 100) / 100),
            money=str(rng.randint(-1_000_000, 9_000_000)),
            gate=rng.choice(gates),
            key=f"ini_{n}",
        )
        group, rule, _ = assign_group(given, CONFIG)
        seen.add(rule)
        assert group in PriorityGroup and 1 <= rule <= 6
        assert (rule == 1) == (given.gate == LegalGate.BLOCKED)
        if group == QW:
            assert given.ease >= CONFIG.ease_threshold and given.effect > 0
    assert seen == {1, 2, 3, 4, 5, 6}


def test_priorities_join_score_rank_and_group() -> None:
    cards = [
        card(key="b", score="70", ease="0.7"),
        card(key="a", score="80", effect="0.5", ease="0.4"),
        card(key="z", gate=LegalGate.BLOCKED, score="95"),
        card(key="c", score="20"),
    ]
    results = build_priorities(cards, CONFIG, assumptions_version=3, config_version=2)
    assert [(r.initiative_id, r.rank, r.group) for r in results] == [
        ("a", 1, ST),
        ("b", 2, QW),
        ("c", 3, NR),  # under the cutoff, but ranked: only a blocked one has no rank
        ("z", None, NR),
    ]
    assert {(r.assumptions_version, r.priority_config_version) for r in results} == {(3, 2)}
    assert all(r.layer == KnowledgeLayer.HYPOTHESIS for r in results)
