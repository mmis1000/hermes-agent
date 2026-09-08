from solver import Rule, Violation, solve


def rules(*items):
    return [Rule(path, mode) for path, mode in items]


def test_rw_subtree_inherits_deeper_parent_ro_carveout():
    result = solve(
        rules(("/a", "rw"), ("/a/b/c", "ro")),
        rules(("/a/b", "rw")),
    )

    assert result.accepted
    assert result.rules == (
        Rule("/a/b", "rw"),
        Rule("/a/b/c", "ro"),
    )


def test_broad_ro_child_request_caps_parent_rw_exception():
    result = solve(
        rules(("/a", "ro"), ("/a/b", "rw")),
        rules(("/a", "ro")),
    )

    assert result.accepted
    assert result.rules == (Rule("/a", "ro"),)


def test_child_can_explicitly_select_parent_rw_exception():
    result = solve(
        rules(("/a", "ro"), ("/a/b", "rw")),
        rules(("/a", "ro"), ("/a/b", "rw")),
    )

    assert result.accepted
    assert result.rules == (
        Rule("/a", "ro"),
        Rule("/a/b", "rw"),
    )


def test_child_can_select_only_the_parent_rw_exception():
    result = solve(
        rules(("/a", "ro"), ("/a/b", "rw")),
        rules(("/a/b", "rw")),
    )

    assert result.accepted
    assert result.rules == (Rule("/a/b", "rw"),)


def test_explicit_widening_is_reported_without_hiding_mathematical_intersection():
    result = solve(
        rules(("/a", "ro"), ("/a/b", "rw")),
        rules(("/a", "rw")),
    )

    assert not result.accepted
    assert result.violations == (Violation("/a", "rw", "ro"),)
    assert result.rules == (
        Rule("/a", "ro"),
        Rule("/a/b", "rw"),
    )


def test_request_outside_parent_scope_is_reported():
    result = solve(
        rules(("/a", "ro")),
        rules(("/x", "ro")),
    )

    assert not result.accepted
    assert result.rules == ()
    assert result.violations == (Violation("/x", "ro", "none"),)


def test_policy_order_does_not_change_the_solution():
    expected = (
        Rule("/a/b", "rw"),
        Rule("/a/b/c", "ro"),
        Rule("/a/b/c/d", "rw"),
    )
    parent_a = rules(
        ("/a", "rw"),
        ("/a/b/c", "ro"),
        ("/a/b/c/d", "rw"),
    )
    parent_b = list(reversed(parent_a))
    child_a = rules(("/a/b", "rw"), ("/a/b/c/d", "rw"))
    child_b = list(reversed(child_a))

    assert solve(parent_a, child_a).rules == expected
    assert solve(parent_b, child_b).rules == expected


def test_redundant_equal_mode_nodes_are_removed_from_canonical_output():
    result = solve(
        rules(("/a", "rw"), ("/a/b", "rw")),
        rules(("/a", "rw"), ("/a/b", "rw")),
    )

    assert result.accepted
    assert result.rules == (Rule("/a", "rw"),)


def test_duplicate_paths_and_noncanonical_paths_are_rejected():
    import pytest

    with pytest.raises(ValueError, match="duplicate"):
        solve(rules(("/a", "rw"), ("/a", "ro")), rules(("/a", "ro")))
    with pytest.raises(ValueError, match="canonical absolute"):
        solve(rules(("/a/../b", "rw")), rules(("/a", "ro")))
