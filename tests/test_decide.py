"""Seam: typed answers to one action (docs/design.md §4.7, step 6)."""

import pytest

from agent_fabric.decisions import Decision, decide


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({"useful": "yes"}, Decision("add")),
        ({"useful": "no"}, Decision("skip")),
        ({"useful": "yes", "ref-0": "supports", "ref-1": "says_nothing"}, Decision("add")),
        ({"useful": "yes", "claim-a": "unrelated"}, Decision("add")),
        ({"useful": "yes", "claim-a": "same"}, Decision("duplicate", ("a",))),
        ({"useful": "yes", "doc-0": "yes"}, Decision("duplicate")),
        ({"useful": "yes", "claim-a": "adds"}, Decision("extend", ("a",))),
        ({"useful": "yes", "claim-a": "replaces"}, Decision("supersede", ("a",))),
        ({"useful": "yes", "claim-a": "replaces", "claim-b": "replaces"}, Decision("supersede", ("a", "b"))),
        ({"useful": "yes", "claim-a": "contradicts"}, Decision("conflict", ("a",))),
    ],
    ids=[
        "useful",
        "not-useful",
        "evidence-that-agrees",
        "unrelated-claim",
        "same-as-a-claim",
        "same-as-the-docs",
        "adds-to-a-claim",
        "replaces-a-claim",
        "replaces-two-claims",
        "contradicts-a-claim",
    ],
)
def test_one_answer_makes_one_action(answers: dict[str, str], expected: Decision) -> None:
    assert decide(answers) == expected


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({"useful": "no", "claim-a": "replaces"}, Decision("supersede", ("a",))),
        ({"useful": "no", "claim-a": "contradicts"}, Decision("conflict", ("a",))),
        ({"useful": "no", "claim-a": "same"}, Decision("duplicate", ("a",))),
        ({"useful": "no", "claim-a": "adds"}, Decision("skip")),
        ({"useful": "yes", "claim-a": "replaces", "claim-b": "contradicts"}, Decision("conflict", ("b",))),
        ({"useful": "yes", "claim-a": "same", "claim-b": "contradicts"}, Decision("duplicate", ("a",))),
        ({"useful": "yes", "claim-a": "adds", "claim-b": "replaces"}, Decision("supersede", ("b",))),
    ],
    ids=[
        "a-replacement-is-useful-whatever-it-says",
        "a-contradiction-is-useful-whatever-it-says",
        "a-duplicate-is-dropped-either-way",
        "adding-what-nobody-needs-is-skipped",
        "a-contradiction-beats-a-replacement",
        "the-same-fact-beats-a-contradiction",
        "a-replacement-beats-an-addition",
    ],
)
def test_where_answers_disagree_the_order_decides(answers: dict[str, str], expected: Decision) -> None:
    assert decide(answers) == expected


@pytest.mark.parametrize(
    "answers",
    [
        {"useful": "yes", "ref-0": "contradicts"},
        {"useful": "yes", "ref-0": "supports", "ref-1": "contradicts", "claim-a": "replaces"},
        {"useful": "yes", "ref-0": "contradicts", "claim-a": "contradicts"},
    ],
    ids=["alone", "against-a-claim-it-would-replace", "against-a-claim-it-would-contradict"],
)
def test_a_note_its_own_evidence_contradicts_is_skipped_and_touches_no_claim(answers: dict[str, str]) -> None:
    assert decide(answers) == Decision("skip")
