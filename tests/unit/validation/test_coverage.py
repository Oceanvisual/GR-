"""Coverage report (FR-028, T031)."""

from validation_builders import fid, fragments

from gr.validation.coverage import coverage_report, describes_action


def test_action_detection_by_verb_stems() -> None:
    assert describes_action("Специалист регистрирует заявление")
    assert describes_action("Уведомление направляется заявителю")
    assert not describes_action("Заявителями являются физические лица")


def test_unbound_action_fragments_are_listed() -> None:
    frags = fragments()
    report = coverage_report(
        frags,
        process_fragment_ids=[fid("п. 3.2")],
        cited_fragment_ids=[fid("п. 2.1")],
        non_process_fragment_ids=[fid("разд. IV")],
    )
    uncovered = set(report.uncovered_action_fragments)
    # The procedures nobody extracted are gaps; their actions are verbs of the act.
    assert {fid("п. 3.3.1"), fid("п. 3.4.1"), fid("п. 3.4.2")} <= uncovered
    assert fid("п. 3.2.1") not in uncovered  # inside the process
    assert fid("п. 4.1") not in uncovered  # marked as not describing processes
    assert fid("п. 2.1") not in uncovered  # cited by a fact
    assert fid("п. 4.1") in report.non_process_fragments
    assert 0 < report.fragments_used < report.fragments_total


def test_repealed_fragments_are_not_counted() -> None:
    frags = fragments(**{"п. 3.4.1": {"repealed": True}})
    report = coverage_report(frags, [fid("п. 3.2")], [], [])
    assert fid("п. 3.4.1") not in report.uncovered_action_fragments
    assert report.fragments_total == coverage_report(fragments(), [], [], []).fragments_total - 1


def test_a_bare_heading_is_not_a_gap() -> None:
    # reg261 (T062): «Рассмотрение Заявительных документов» over points already in processes.
    title = "Специалист регистрирует заявление"
    heading = fragments(**{"разд. III": {"heading": title, "text": title}})
    lead_in = fragments(**{"разд. III": {"heading": None, "text": title}})
    assert fid("разд. III") not in coverage_report(heading, [], [], []).uncovered_action_fragments
    assert fid("разд. III") in coverage_report(lead_in, [], [], []).uncovered_action_fragments
