"""Standalone solver for delegated reveal-policy tree intersection.

This is a throwaway spike, not production Hermes code.  A policy is a set of
absolute path rules using longest-prefix precedence.  ``none < ro < rw`` and
the effective child policy is the pointwise minimum of parent and child.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import PurePosixPath
from typing import Iterable, Sequence


_MODE_RANK = {"none": 0, "ro": 1, "rw": 2}


@dataclass(frozen=True)
class Rule:
    path: str
    mode: str


@dataclass(frozen=True)
class Violation:
    path: str
    requested: str
    parent_effective: str


@dataclass(frozen=True)
class Solution:
    rules: tuple[Rule, ...]
    violations: tuple[Violation, ...]

    @property
    def accepted(self) -> bool:
        return not self.violations


def _normalize_path(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("rule path must be a string")
    candidate = PurePosixPath(raw)
    if (
        not candidate.is_absolute()
        or candidate == PurePosixPath("/")
        or ".." in candidate.parts
        or raw != str(candidate)
    ):
        raise ValueError(f"rule path must be canonical absolute POSIX path: {raw!r}")
    return str(candidate)


def _normalize_rules(raw_rules: Iterable[Rule]) -> tuple[Rule, ...]:
    normalized: list[Rule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, Rule):
            raise ValueError("rules must contain Rule objects")
        path = _normalize_path(raw.path)
        if raw.mode not in {"ro", "rw"}:
            raise ValueError(f"rule mode must be ro or rw: {raw.mode!r}")
        if path in seen:
            raise ValueError(f"duplicate rule path: {path}")
        seen.add(path)
        normalized.append(Rule(path, raw.mode))
    return tuple(sorted(normalized, key=_rule_sort_key))


def _rule_sort_key(rule: Rule) -> tuple[int, str]:
    path = PurePosixPath(rule.path)
    return (len(path.parts), rule.path)


def _contains(root: str, candidate: str) -> bool:
    root_path = PurePosixPath(root)
    candidate_path = PurePosixPath(candidate)
    return candidate_path == root_path or root_path in candidate_path.parents


def effective_mode(rules: Sequence[Rule], path: str) -> str:
    """Return the longest-prefix mode at ``path``, or ``none``."""

    path = _normalize_path(path)
    matches = [rule for rule in rules if _contains(rule.path, path)]
    if not matches:
        return "none"
    return max(matches, key=lambda rule: len(PurePosixPath(rule.path).parts)).mode


def _lower_mode(left: str, right: str) -> str:
    return left if _MODE_RANK[left] <= _MODE_RANK[right] else right


def solve(parent: Iterable[Rule], child: Iterable[Rule]) -> Solution:
    """Intersect parent and requested child path-policy trees.

    The returned rules are a canonical transition list.  Explicit child rules
    that exceed the parent's effective mode at that exact path are reported as
    violations, while ``rules`` still exposes the mathematical intersection so
    the admission decision and policy calculation can be examined separately.
    """

    parent_rules = _normalize_rules(parent)
    child_rules = _normalize_rules(child)

    violations = tuple(
        Violation(rule.path, rule.mode, parent_mode)
        for rule in child_rules
        for parent_mode in (effective_mode(parent_rules, rule.path),)
        if _MODE_RANK[rule.mode] > _MODE_RANK[parent_mode]
    )

    candidate_paths = sorted(
        {rule.path for rule in parent_rules}.union(rule.path for rule in child_rules),
        key=lambda path: (len(PurePosixPath(path).parts), path),
    )
    output: list[Rule] = []
    for path in candidate_paths:
        mode = _lower_mode(
            effective_mode(parent_rules, path),
            effective_mode(child_rules, path),
        )
        if mode == "none":
            continue
        if effective_mode(output, path) != mode:
            output.append(Rule(path, mode))

    return Solution(tuple(output), violations)


def _parse_cli_rule(value: str) -> Rule:
    try:
        path, mode = value.rsplit("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("rule must be PATH=ro or PATH=rw") from exc
    return Rule(path, mode)


def _solution_payload(solution: Solution) -> dict:
    return {
        "accepted": solution.accepted,
        "rules": [asdict(rule) for rule in solution.rules],
        "violations": [asdict(item) for item in solution.violations],
    }


def _demo_payload() -> list[dict]:
    scenarios = [
        (
            "inherit-deeper-ro",
            (Rule("/a", "rw"), Rule("/a/b/c", "ro")),
            (Rule("/a/b", "rw"),),
        ),
        (
            "broad-ro-caps-rw-exception",
            (Rule("/a", "ro"), Rule("/a/b", "rw")),
            (Rule("/a", "ro"),),
        ),
        (
            "explicitly-select-rw-exception",
            (Rule("/a", "ro"), Rule("/a/b", "rw")),
            (Rule("/a", "ro"), Rule("/a/b", "rw")),
        ),
        (
            "reject-explicit-root-widening",
            (Rule("/a", "ro"), Rule("/a/b", "rw")),
            (Rule("/a", "rw"),),
        ),
    ]
    return [
        {
            "name": name,
            "parent": [asdict(rule) for rule in parent],
            "child": [asdict(rule) for rule in child],
            "solution": _solution_payload(solve(parent, child)),
        }
        for name, parent, child in scenarios
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", action="append", type=_parse_cli_rule, default=[])
    parser.add_argument("--child", action="append", type=_parse_cli_rule, default=[])
    args = parser.parse_args(argv)

    if not args.parent and not args.child:
        print(json.dumps(_demo_payload(), indent=2))
        return 0
    if not args.parent or not args.child:
        parser.error("provide at least one --parent and one --child rule")
    solution = solve(args.parent, args.child)
    print(json.dumps(_solution_payload(solution), indent=2))
    return 0 if solution.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
