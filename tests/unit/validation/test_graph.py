"""Operation graph: references, cycles, order (FR-026, T031)."""

from gr.validation.graph import Node, check_graph


def nodes(*spec: tuple[str, tuple[str, ...]] | tuple[str, tuple[str, ...], bool]) -> list[Node]:
    return [Node(item[0], item[1], len(item) == 3 and bool(item[-1])) for item in spec]


def test_topological_order_with_ties_in_answer_order() -> None:
    result = check_graph(nodes(("c", ("a", "b")), ("b", ()), ("a", ()), ("d", ("a",))))
    assert result.order == ["b", "a", "c", "d"]
    assert result.cycles == [] and result.dangling == []


def test_missing_and_self_dependencies_are_dropped() -> None:
    result = check_graph(nodes(("a", ("zz", "a")), ("b", ("a", "a"))))
    assert result.dangling == [("a", "zz")]
    assert result.depends_on == {"a": [], "b": ["a"]}


def test_cycle_without_loop_back_is_reported_and_keeps_answer_order() -> None:
    result = check_graph(nodes(("x", ()), ("a", ("c",)), ("b", ("a",)), ("c", ("b",))))
    assert result.cycles == [["a", "b", "c"]]
    assert result.order == ["x", "a", "b", "c"]


def test_rework_loop_is_allowed_and_ordered_after_its_target() -> None:
    result = check_graph(
        nodes(("check", ("intake", "fix")), ("intake", ()), ("fix", ("check",), True))
    )
    assert result.cycles == []
    assert result.order == ["intake", "check", "fix"]


def test_long_chain_is_not_recursive() -> None:
    chain = [Node(f"o{i}", (f"o{i - 1}",) if i else ()) for i in range(5000)]
    assert check_graph(chain).order[-1] == "o4999"
