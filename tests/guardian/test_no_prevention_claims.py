"""G-44 (#48): the product never claims it can stop an attack.

It detects and it tells you. It has no power to freeze, cancel, block or
reverse anything, and a customer who believes otherwise will not act on an
alert with the urgency it needs — they will assume something is already being
done. The alert only has value because the customer acts on it, so a
prevention claim does not merely oversell the product, it disarms it.

The claim is true today. Nothing kept it that way: it held because whoever
wrote the copy happened to be careful, which is the same as not holding. This
is the part of #48 that can be enforced rather than remembered.

## What counts as a claim, and why the first two attempts were wrong

A verb alone is not a claim. Both false positives found while writing this are
sentences the product *must* keep saying:

- `"It cannot stop an attack"` — the limits page, denying the very thing the
  guard looks for. A guard that fires on the honest page gets suppressed, and
  a suppressed guard protects nothing.
- `"If you did not authorise this, freeze the account in the Bitget app."` —
  the runbook, telling the *customer* to act. That is the honest model stated
  out loud, and flagging it would push the copy towards vagueness.

So a claim needs three things together: the product as the subject, the verb,
and no negation governing it — all inside the same clause. "We freeze your
account" is a claim; "freeze the account in the app" is advice; "it cannot
freeze anything" is the truth.

**Why string literals and not `grep`.** A grep cannot tell copy from a comment
explaining why the copy avoids a word — this very file would trip it, and so
would the docstring you are reading. The scan walks the AST and reads only the
strings that can reach a page.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# Files whose strings a customer can read. The ops dashboard is deliberately
# absent: it is read by operators, where "blocked" means a CSRF check did its
# job and carries no promise to anybody.
CUSTOMER_FACING = [
    ROOT / "guardian" / "alerts" / "base.py",
]

# The intervening verbs. Each is only a claim once a product subject and the
# absence of a negation are established below.
PREVENTION_VERBS = [
    r"\bprevent(s|ed|ing|ion)?\b",
    r"\bstop(s|ped|ping)? (an?|the|any|your) (attack|theft|hack|withdrawal|intruder)",
    r"\bblock(s|ed|ing)? (an?|the|any|your) (attack|theft|hack|withdrawal|intruder)",
    r"\bfreez(e|es|ing) (your|the) (account|funds|withdrawal)",
    r"\b(protect|secure|safeguard)(s|ed|ing)? your (funds|money|assets)\b",
    r"\bkeep(s|ing)? your (funds|money|assets) safe\b",
    r"\bcancel(s|led|ling)? (an?|the) (withdrawal|transfer|trade)",
    r"\breverse(s|d)? (an?|the) (withdrawal|transfer|transaction)",
]

# Us doing it, rather than the customer being told to.
PRODUCT_SUBJECT = re.compile(
    r"\b(we|our|us|minas|the guardian|guardian|sentinel|this service|the service|it)\b",
    re.I,
)

NEGATION = re.compile(
    r"\b(cannot|can not|can't|could not|couldn't|does not|doesn't|do not|don't|"
    r"did not|didn't|will not|won't|never|no power to|not able to|unable to|"
    r"nothing that|no way to)\b",
    re.I,
)

# Where one clause stops governing the next. "We cannot promise uptime, but the
# Guardian prevents theft" must still be caught: the negation belongs to the
# first clause and has no reach into the second.
CLAUSE_BREAK = re.compile(r"[.;:,]|\bbut\b|\bhowever\b|\balthough\b|\bwhereas\b", re.I)


def _clause_before(text: str, at: int) -> str:
    """The run of text governing the match: back to the last clause break."""
    head = text[:at]
    breaks = list(CLAUSE_BREAK.finditer(head))
    return head[breaks[-1].end():] if breaks else head


def claims_in(text: str) -> list[str]:
    """The patterns this text trips as an actual claim.

    The single definition of the rule. The scanner and every test below come
    through here, so a false-positive test cannot end up asserting about a rule
    the guard does not use.
    """
    hits = []
    for pattern in PREVENTION_VERBS:
        for match in re.finditer(pattern, text, re.I):
            clause = _clause_before(text, match.start())
            if not PRODUCT_SUBJECT.search(clause):
                continue  # advice to the customer, not a claim about us
            if NEGATION.search(clause):
                continue  # a denial of the claim is not the claim
            hits.append(pattern)
    return hits


def _customer_visible_strings(path: Path):
    """Every string literal that is not a docstring, with its line number."""
    tree = ast.parse(path.read_text())

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


@pytest.mark.parametrize("path", CUSTOMER_FACING, ids=lambda p: p.name)
def test_no_prevention_claim_reaches_a_customer(path: Path):
    offences = [
        f"{path.name}:{lineno}: {text.strip()[:120]!r} matches /{pattern}/"
        for lineno, text in _customer_visible_strings(path)
        for pattern in claims_in(text)
    ]

    assert offences == [], (
        "The product must never claim it can stop an attack — it detects and "
        "tells you, and a customer who believes otherwise will not act on the "
        "alert with the urgency that decides the outcome (G-44, #48).\n  "
        + "\n  ".join(offences)
    )


def test_every_pattern_catches_the_claim_it_is_for():
    """A scanner nobody has seen fire is a scanner that might match nothing.
    Each pattern gets a sentence it must catch, so a typo in one cannot leave
    that claim silently unguarded."""
    samples = [
        "The Guardian prevents unauthorised withdrawals.",
        "We stop an attack before it completes.",
        "Minas blocks the withdrawal automatically.",
        "We freeze your account the moment we see it.",
        "The Guardian protects your funds around the clock.",
        "We keep your money safe.",
        "We cancel the withdrawal for you.",
        "Minas can reverse a transfer.",
    ]
    assert len(samples) == len(PREVENTION_VERBS), "every pattern needs a sample that trips it"

    for pattern, sample in zip(PREVENTION_VERBS, samples, strict=True):
        assert pattern in claims_in(sample), f"/{pattern}/ did not match {sample!r}"


def test_advice_to_the_customer_is_not_a_claim_about_us():
    """The runbook's whole job is to tell a frightened person what to do. It
    uses exactly these verbs, and flagging it would push the most important
    copy in the product towards vagueness."""
    advice = [
        "If you did not authorise this, freeze the account in the Bitget app.",
        "Revoke the API key at your exchange, then contact their support.",
        "Protect your account by turning on two-factor authentication.",
        "Cancel the withdrawal from your exchange's app if it is still pending.",
    ]
    for sentence in advice:
        assert claims_in(sentence) == [], f"advice flagged as a claim: {sentence!r}"


def test_a_denial_of_prevention_is_not_a_claim_of_it():
    """The sentence the product most needs to say contains the words the guard
    looks for."""
    denials = [
        "It cannot stop an attack; it tells you one is happening.",
        "The Guardian will never block a withdrawal.",
        "We have no power to freeze your account.",
        "Minas cannot reverse a transfer.",
    ]
    for sentence in denials:
        assert claims_in(sentence) == [], f"a denial was read as a claim: {sentence!r}"


def test_a_negation_in_an_earlier_clause_does_not_launder_the_claim():
    """Clause-scoped on purpose. A caveat about something else must not buy
    cover for the sentence after it."""
    text = "We cannot promise uptime, but the Guardian prevents unauthorised withdrawals."
    assert claims_in(text), "the claim after the clause break was let through"


def test_the_ops_dashboard_is_not_scanned_and_that_is_deliberate():
    """Operators read 'blocked' as a CSRF check doing its job. Scanning it
    would either fire on true statements or force the wordlist so narrow it
    stops catching customer-facing marketing."""
    assert not any(p.name == "dashboard.py" for p in CUSTOMER_FACING)


