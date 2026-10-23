"""Performer mentioned in the quote (R-11, FR-025, T026)."""

import pytest

from gr.grounding.roles import role_in_quote, role_score

QUOTE = "Документы принимаются должностным лицом, ответственным за прием документов."


@pytest.mark.parametrize(
    ("role", "quote"),
    [
        ("должностное лицо, ответственное за прием", QUOTE),  # the table of tasks.md
        ("должностное лицо", QUOTE),
        ("специалист Росимущества", "Специалистом Росимущества осуществляется проверка"),
        ("Росимущество", "специалист Росимущества проверяет"),
        ("МФЦ", "заявление подаётся через МФЦ по месту жительства"),
        ("заявитель", "заявителю направляется уведомление"),
        ("уполномоченный орган", "решение принимает Уполномоченный Орган"),
        (
            "руководитель (заместитель руководителя)",
            "подписывается руководителем или заместителем руководителя",
        ),
        ("начальник отдела", "начальником отдела земельных отношений"),
    ],
)
def test_role_is_found_in_any_case(role: str, quote: str) -> None:
    assert role_in_quote(role, quote)


@pytest.mark.parametrize(
    ("role", "quote"),
    [
        ("руководитель", QUOTE),  # the table of tasks.md
        ("заявитель", "заявление регистрируется в день поступления"),  # one letter apart
        ("МФЦ", "многофункциональный центр"),  # abbreviations are not expanded
        ("руководитель территориального органа", "территориальный орган рассматривает"),
        ("специалист отдела", "специалист проверяет документы"),  # half the role is missing
        ("", QUOTE),
        ("и в за", QUOTE),  # nothing but function words
    ],
)
def test_role_is_not_found_without_its_words(role: str, quote: str) -> None:
    assert not role_in_quote(role, quote)


def test_score_is_the_share_of_role_words() -> None:
    assert role_score("начальник отдела кадров", "начальником отдела") == pytest.approx(2 / 3)
