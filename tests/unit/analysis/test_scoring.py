"""Priority Score: normalisation, breakdown, rank (FR-060…FR-062, FR-065, data-model §6, T047)."""

import random
from decimal import Decimal
from typing import Any

import pytest
from initiative_builders import initiative

from gr.analysis.scoring import load_priority_config, rank, score_card, score_initiatives
from gr.domain.economics import EconomicsResult, Effect, InitiativeEconomics
from gr.domain.enums import ReviewStatus, Scenario, SolutionCategory
from gr.domain.initiative import Initiative
from gr.domain.priority import CRITERIA, Criterion, PriorityConfig
from gr.domain.review import ReviewState

CONFIG = load_priority_config()


def make(
    key: str = "ini_1",
    *,
    feasibility: str = "medium",
    data: str = "medium",
    integration: str = "medium",
    legal: str = "medium",
    operational: str = "medium",
    duration: str = "m3_6",
    gate: str = "allowed",
    justify: bool = False,
    **updates: Any,
) -> Initiative:
    def rating(level: str) -> dict[str, str]:
        return {"level": level, "justification": "x"}

    return initiative(
        SolutionCategory.HYBRID,
        id=key,
        ratings={
            "feasibility": rating(feasibility),
            "data_availability": rating(data),
            "integration_complexity": rating(integration),
            "legal_risk": rating(legal),
            "operational_risk": rating(operational),
            "duration": {"bucket": duration, "justification": "x"},
        },
        gate={"status": gate, "reasons": []},
        tech_guard={"min_category": "classic_automation", "requires_justification": justify},
        **updates,
    )


def economics(effects: dict[str, str], scenario: Scenario = Scenario.BASE) -> EconomicsResult:
    def effect(value: Decimal) -> Effect:
        zero = [Decimal(0)]
        return Effect(hours=zero, fte=zero, money=zero, opex=zero, capex=Decimal(0),
                      net=[value], cum_net=value)  # fmt: skip

    return EconomicsResult(
        scenario=scenario,
        assumptions_version=1,
        per_initiative=[
            InitiativeEconomics(initiative_id=key, solo=effect(Decimal(value)))
            for key, value in effects.items()
        ],
    )


def weights_only(criterion: Criterion) -> PriorityConfig:
    weights = dict.fromkeys(CRITERIA, Decimal(0)) | {criterion: Decimal(7)}
    return CONFIG.model_copy(update={"weights": weights})


def test_shipped_defaults_are_those_of_the_data_model() -> None:
    assert PriorityConfig() == CONFIG
    assert sum(CONFIG.weights.values()) == 100 and CONFIG.effect_ref == 5_000_000


def test_hand_computed_score_and_breakdown() -> None:
    # effect 2 500 000 / 5 000 000 = 0.5; feasibility high 0.75; data medium 0.5;
    # integration low → 1 − 0.25 = 0.75; risk = 1 − max(low 0.25, high 0.75) = 0.25;
    # duration 3–6 months 0.7. Weights 30/20/15/10/15/10 out of 100:
    # 15 + 15 + 7.5 + 7.5 + 3.75 + 7 = 55.75 → 55.8 (half to even). ease = (0.75+0.75+0.7)/3.
    card = score_card(
        make(feasibility="high", integration="low", legal="low", operational="high"),
        Decimal(2_500_000),
        CONFIG,
    )
    assert card.score == Decimal("55.8")
    assert round(card.ease, 6) == Decimal("0.733333")
    assert {k: c.contribution for k, c in card.criteria.items()} == {
        "effect": Decimal(15),
        "feasibility": Decimal(15),
        "data_availability": Decimal("7.5"),
        "integration": Decimal("7.5"),
        "risk": Decimal("3.75"),
        "duration": Decimal(7),
    }
    assert card.criteria["risk"].raw == "high"  # the worse of the two risks
    assert card.criteria["effect"].raw == Decimal(2_500_000)
    assert card.criteria["integration"].normalized == Decimal("0.75")


def test_the_breakdown_recomputes_the_score() -> None:
    rng = random.Random(7)
    levels = ["very_low", "low", "medium", "high", "very_high"]
    for _ in range(200):
        item = make(
            feasibility=rng.choice(levels),
            data=rng.choice(levels),
            integration=rng.choice(levels),
            legal=rng.choice(levels),
            operational=rng.choice(levels),
            duration=rng.choice(["lt_3m", "m3_6", "m6_12", "gt_12m"]),
            justify=rng.random() < 0.3,
        )
        card = score_card(item, Decimal(rng.randint(-2_000_000, 8_000_000)), CONFIG)
        criteria = card.criteria.values()
        recomputed = (
            100 * sum(c.weight * c.normalized for c in criteria) / sum(c.weight for c in criteria)
        )
        assert recomputed == sum(c.contribution for c in criteria)
        assert round(recomputed, 1) == card.score
        assert Decimal(0) <= card.score <= 100


@pytest.mark.parametrize("criterion", CRITERIA)
def test_one_weight_makes_the_score_that_criterion(criterion: Criterion) -> None:
    card = score_card(make(feasibility="high", duration="m6_12"), Decimal(1_000_000),
                      weights_only(criterion))  # fmt: skip
    assert card.score == round(100 * card.criteria[criterion].normalized, 1)


@pytest.mark.parametrize(
    ("effect", "normalised"),
    [("-300000", "0"), ("0", "0"), ("1000000", "0.2"), ("5000000", "1"), ("12000000", "1")],
)
def test_effect_is_absolute_and_clamped(effect: str, normalised: str) -> None:
    card = score_card(make(), Decimal(effect), CONFIG)
    assert card.criteria["effect"].normalized == Decimal(normalised)


def test_tech_guard_penalty_lowers_feasibility_not_below_zero() -> None:
    plain = score_card(make(feasibility="high"), Decimal(0), CONFIG)
    flagged = score_card(make(feasibility="high", justify=True), Decimal(0), CONFIG)
    floor = score_card(make(feasibility="very_low", justify=True), Decimal(0), CONFIG)
    assert plain.criteria["feasibility"].normalized == Decimal("0.75")
    assert flagged.criteria["feasibility"].normalized == Decimal("0.5")
    assert floor.criteria["feasibility"].normalized == 0


def test_best_ratings_and_full_effect_score_100() -> None:
    best = make(feasibility="very_high", data="very_high", integration="very_low",
                legal="very_low", operational="very_low", duration="lt_3m")  # fmt: skip
    assert score_card(best, Decimal(5_000_000), CONFIG).score == 100


def test_ties_go_by_effect_then_id_and_blocked_has_no_rank() -> None:
    cards = score_initiatives(
        [make("b"), make("a"), make("c"), make("z", gate="blocked"), make("d", data="high")],
        economics({"a": "200", "b": "100", "c": "200", "z": "999999", "d": "0"}),
        CONFIG,
    )
    assert {c.score for c in cards if c.initiative_id in "abc"} == {cards[0].score}
    assert rank(cards) == {"d": 1, "a": 2, "c": 3, "b": 4, "z": None}


def test_adding_an_initiative_changes_no_other_score() -> None:
    first = score_initiatives([make("a"), make("b", data="low")],
                              economics({"a": "1000000", "b": "3000000"}), CONFIG)  # fmt: skip
    more = score_initiatives(
        [make("a"), make("b", data="low"), make("c", feasibility="very_high")],
        economics({"a": "1000000", "b": "3000000", "c": "9000000"}),
        CONFIG,
    )
    assert first == more[:2]
    assert rank(first)["a"] != rank(more)["a"]  # the rank may move, the score may not


def test_input_order_does_not_matter() -> None:
    items = [make(k, feasibility=level) for k, level in zip("abcde", ["low", "high", "medium",
             "high", "very_low"], strict=True)]  # fmt: skip
    effects = economics({k: str(n * 700_000) for n, k in enumerate("abcde")})
    cards = score_initiatives(items, effects, CONFIG)
    shuffled = score_initiatives(random.Random(3).sample(items, 5), effects, CONFIG)
    assert cards == shuffled and rank(cards) == rank(shuffled)


def test_inputs_checked() -> None:
    with pytest.raises(ValueError, match="base scenario"):
        score_initiatives([make()], economics({"ini_1": "1"}, Scenario.OPTIMISTIC), CONFIG)
    rejected = make("r", review=ReviewState(status=ReviewStatus.REJECTED))
    assert score_initiatives([rejected], economics({}), CONFIG) == []  # no economics, no card
    with pytest.raises(ValueError, match="no legal gate"):
        score_card(make(gate="allowed").model_copy(update={"gate": None}), Decimal(0), CONFIG)
