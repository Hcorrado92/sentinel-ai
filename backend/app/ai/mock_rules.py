"""Default rule table of the mock provider: the demo application's three faults.

Each ``DemoFault`` is keyed by the exception type plus the file and function of
the faulting frame in the stack trace (``demo_app/main.py``). It yields:

- an RCA whose hypotheses cite the stack frames and the code excerpt, with a
  confidence below 1, uncertainties and next steps;
- a ``PatchModel`` generated from the *current* file contents of the isolated
  copy, read with ``RepoReader``: an anchored find-and-replace of the faulty
  snippet, a regression test under ``tests/`` and the removal of the matching
  ``fault_reproduction`` tests (only ever in the copy). A missing or ambiguous
  anchor raises ``PatchAnchorNotFound`` (a ``NoPatchAvailable``), so the repair
  stops instead of guessing.

Incidents that match no fault get the generic RCA and ``no_patch_available``.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.ai.base import NoPatchAvailable, Prompt
from app.ai.mock import MockRule, _incident_label, _is_patch_request, _is_rca_request
from app.repair.patch import FileAction, FileChange, PatchModel
from app.repo_tools import RepoAccessError, RepoReader

DEMO_FILE = "demo_app/main.py"
REPRODUCTION_TESTS = "tests/test_fault_reproduction.py"
REDACTED = "[REDACTED]"

_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)\s*$')


class PatchAnchorNotFound(NoPatchAvailable):
    """The snippet a mock patch is anchored to is missing (or ambiguous) in the file."""


@dataclass(frozen=True)
class Replacement:
    anchor: str
    replacement: str


@dataclass(frozen=True)
class RemovedTest:
    """A ``fault_reproduction`` test to remove; ``expects`` must appear in its body."""

    name: str
    expects: str


@dataclass(frozen=True)
class DemoFault:
    key: str
    exception_type: str
    function: str
    code_marker: str
    title: str
    description: str
    confidence: float
    uncertainties: tuple[str, ...]
    next_steps: tuple[str, ...]
    fix: Replacement
    regression_test: str
    removed_tests: tuple[RemovedTest, ...]
    explanation: str

    @property
    def regression_test_path(self) -> str:
        return f"tests/test_regression_{self.key}.py"


@dataclass(frozen=True)
class _Frame:
    path: str
    line: int
    function: str
    source: str


DEMO_FAULTS: tuple[DemoFault, ...] = (
    DemoFault(
        key="null_nickname",
        exception_type="AttributeError",
        function="_display_name",
        code_marker='user["nickname"].upper()',
        title="Optional nickname (None) is used as a string in _display_name",
        description=(
            "_display_name calls .upper() on user['nickname'] without checking it. The "
            "nickname is optional and is None for some users, so the call raises "
            "AttributeError and the profile endpoint answers 500."
        ),
        confidence=0.85,
        uncertainties=(
            "Other optional user fields may have the same unchecked usage.",
        ),
        next_steps=(
            "Fall back to the user's name when the nickname is None.",
            "Add a regression test requesting the profile of a user without a nickname.",
        ),
        fix=Replacement(
            anchor=(
                "    if faults_enabled:\n"
                "        # FAULT 1: nickname is optional and may be None.\n"
                '        return user["nickname"].upper()  # type: ignore[union-attr]\n'
                '    return (user["nickname"] or user["name"] or "").upper()\n'
            ),
            replacement=(
                "    # Sentinel fix: the nickname is optional (None); fall back to the name.\n"
                '    return (user["nickname"] or user["name"] or "").upper()\n'
            ),
        ),
        regression_test=(
            '"""Regression test added by Sentinel AI: a user without a nickname has a profile."""\n'
            "\n\n"
            "def test_profile_without_nickname_falls_back_to_name(faulty_client):\n"
            '    response = faulty_client.get("/users/2/profile")\n'
            "    assert response.status_code == 200\n"
            '    assert response.json()["display_name"] == "BRUNO SAMPLE"\n'
        ),
        removed_tests=(
            RemovedTest("test_profile_with_missing_nickname_returns_500", '"/users/2/profile"'),
            RemovedTest("test_faults_enabled_from_environment_reproduce", '"/users/2/profile"'),
        ),
        explanation=(
            "[mock] _display_name used the optional nickname as a string. It now falls "
            "back to the user's name when the nickname is None. Adds a regression test "
            "and removes the reproduction tests that expected the 500."
        ),
    ),
    DemoFault(
        key="quantity_validation",
        exception_type="ValueError",
        function="_parse_quantity",
        code_marker="quantity = int(raw)",
        title="Unvalidated int() conversion of quantity in _parse_quantity",
        description=(
            "_parse_quantity converts the request's quantity with int() without handling "
            "ValueError. A non-numeric quantity escapes as an unhandled exception (500) "
            "instead of a validation error (422)."
        ),
        confidence=0.8,
        uncertainties=(
            "The OrderIn model accepts strings for quantity; stricter typing could also fix it.",
        ),
        next_steps=(
            "Catch the conversion error and answer 422 with a clear message.",
            "Add a regression test posting a non-numeric quantity.",
        ),
        fix=Replacement(
            anchor=(
                "    if faults_enabled:\n"
                "        # FAULT 2: unvalidated conversion; ValueError escapes as a 500.\n"
                "        quantity = int(raw)\n"
                "    else:\n"
                "        try:\n"
                "            quantity = int(raw)\n"
                "        except (TypeError, ValueError):\n"
                '            raise HTTPException(status_code=422, detail="quantity must be an integer")\n'
            ),
            replacement=(
                "    # Sentinel fix: validate the conversion and answer 422 instead of a 500.\n"
                "    try:\n"
                "        quantity = int(raw)\n"
                "    except (TypeError, ValueError):\n"
                '        raise HTTPException(status_code=422, detail="quantity must be an integer")\n'
            ),
        ),
        regression_test=(
            '"""Regression test added by Sentinel AI: a non-numeric quantity is a 422."""\n'
            "\n\n"
            "def test_non_numeric_quantity_is_rejected_with_422(faulty_client):\n"
            '    response = faulty_client.post("/orders", json={"product": "widget", "quantity": "three"})\n'
            "    assert response.status_code == 422\n"
        ),
        removed_tests=(
            RemovedTest("test_order_with_non_numeric_quantity_returns_500", '"three"'),
        ),
        explanation=(
            "[mock] _parse_quantity let the ValueError of int() escape as a 500. The "
            "conversion is now validated and answers 422. Adds a regression test and "
            "removes the reproduction test that expected the 500."
        ),
    ),
    DemoFault(
        key="discount_calculation",
        exception_type="DiscountError",
        function="apply_discount",
        code_marker="raise DiscountError(",
        title="Fixed discount larger than the subtotal is not capped in apply_discount",
        description=(
            "apply_discount subtracts a fixed coupon amount from the subtotal without "
            "capping it. When the coupon exceeds the subtotal the total is negative and "
            "DiscountError is raised, so the endpoint answers 500."
        ),
        confidence=0.75,
        uncertainties=(
            "The intended business rule (cap at zero or reject the coupon) is not documented.",
        ),
        next_steps=(
            "Cap the discounted total at zero.",
            "Add a regression test applying a coupon larger than the subtotal.",
        ),
        fix=Replacement(
            anchor=(
                "    if faults_enabled:\n"
                "        # FAULT 3: a fixed discount larger than the subtotal is not capped.\n"
                "        if total < 0:\n"
                '            raise DiscountError(f"discount produced a negative total: {total}")\n'
                "    else:\n"
                "        total = max(total, 0.0)\n"
            ),
            replacement=(
                "    # Sentinel fix: cap the discount so the total is never negative.\n"
                "    total = max(total, 0.0)\n"
            ),
        ),
        regression_test=(
            '"""Regression test added by Sentinel AI: a large coupon never gives a negative total."""\n'
            "\n"
            "from demo_app.main import apply_discount\n"
            "\n\n"
            "def test_oversized_fixed_coupon_gives_zero_total(faulty_client):\n"
            '    response = faulty_client.get("/orders/1/total", params={"coupon": "BIGDEAL"})\n'
            "    assert response.status_code == 200\n"
            '    assert response.json()["total"] == 0.0\n'
            "\n\n"
            "def test_apply_discount_never_returns_a_negative_total():\n"
            '    assert apply_discount(30.0, "BIGDEAL", faults_enabled=True) == 0.0\n'
        ),
        removed_tests=(
            RemovedTest("test_total_with_oversized_coupon_returns_500", '"BIGDEAL"'),
        ),
        explanation=(
            "[mock] apply_discount did not cap a fixed discount larger than the subtotal "
            "and raised DiscountError. The total is now capped at zero. Adds a regression "
            "test and removes the reproduction test that expected the 500."
        ),
    ),
)


# -- matching -------------------------------------------------------------------


def _is_demo_file(path: str) -> bool:
    normalized = (path or "").replace("\\", "/")
    return normalized == DEMO_FILE or normalized.endswith("/" + DEMO_FILE)


def _same_exception(prompt: Prompt, fault: DemoFault) -> bool:
    exc_type = str(prompt.context.get("exception_type") or "")
    return exc_type.rsplit(".", 1)[-1] == fault.exception_type


def _frames(stack_trace: str) -> list[_Frame]:
    """Application frames of a Python traceback (no libraries), innermost first."""
    lines = (stack_trace or "").replace("\r\n", "\n").split("\n")
    frames: list[_Frame] = []
    for index, text in enumerate(lines):
        match = _FRAME.match(text)
        if not match:
            continue
        path = match.group("path")
        if path.startswith("<") or "site-packages" in path:
            continue
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if following.startswith("File ") or not following.strip("~^ "):
            following = ""
        frames.append(
            _Frame(path.replace("\\", "/"), int(match.group("line")), match.group("func"), following)
        )
    return list(reversed(frames))


def _fault_frame(fault: DemoFault, prompt: Prompt) -> _Frame | None:
    if not _same_exception(prompt, fault):
        return None
    for frame in _frames(str(prompt.context.get("stack_trace") or "")):
        if _is_demo_file(frame.path):
            # The innermost demo_app/main.py frame must be the faulty function.
            return frame if frame.function == fault.function else None
    return None


def _suspect(fault: DemoFault, prompt: Prompt) -> dict[str, Any] | None:
    if not _same_exception(prompt, fault):
        return None
    for location in prompt.context.get("suspect_locations") or []:
        if not isinstance(location, dict):
            continue
        if _is_demo_file(str(location.get("file") or "")) and location.get("function") == fault.function:
            return location
    return None


# -- RCA ------------------------------------------------------------------------


def _excerpt_line(prompt: Prompt, frame: _Frame, marker: str) -> tuple[str, int, str] | None:
    """The cited code line (path, line number, text) from the RCA excerpts."""
    for excerpt in prompt.context.get("excerpts") or []:
        path = str(excerpt.get("path") or "")
        if not _is_demo_file(path):
            continue
        start = int(excerpt.get("start_line") or 1)
        lines = str(excerpt.get("content") or "").replace("\r\n", "\n").split("\n")
        offset = frame.line - start
        if 0 <= offset < len(lines) and lines[offset].strip():
            return path, frame.line, lines[offset].strip()
        for number, text in enumerate(lines, start=start):
            if marker in text:
                return path, number, text.strip()
    return None


def _rca_output(fault: DemoFault, prompt: Prompt) -> dict[str, Any]:
    ctx = prompt.context
    frame = _fault_frame(fault, prompt)
    assert frame is not None  # guaranteed by the rule's matcher
    top = ctx.get("top_frame") or {}
    file = str(top.get("file")) if _is_demo_file(str(top.get("file") or "")) else DEMO_FILE
    demo_frames = [f for f in _frames(str(ctx.get("stack_trace") or "")) if _is_demo_file(f.path)]
    chain = " -> ".join(f.function for f in reversed(demo_frames))
    label = _incident_label(prompt)

    evidence = [
        f"Stack frame: {file}:{frame.line} in {frame.function} (innermost application frame)",
        f"Application call chain in the stack trace: {chain}",
    ]
    if frame.source:
        evidence.append(f"Failing line in the traceback: {frame.source}")
    uncertainties = [
        "Generated by the deterministic mock provider from a known demo fault: no model "
        "reasoning was performed.",
        "The root cause is not confirmed until a repair verified by tests supports it.",
        *fault.uncertainties,
    ]
    cited = _excerpt_line(prompt, frame, fault.code_marker)
    if cited is not None:
        evidence.append(f"Code excerpt {cited[0]}:{cited[1]}: {cited[2]}")
    else:
        uncertainties.append(f"No repository excerpt of {file} was available to inspect the code.")
    evidence.append(f"Exception reported: {label}")
    count = ctx.get("event_count")
    if count:
        evidence.append(f"Observed in {count} event(s) grouped under the same fingerprint")

    switch_evidence = [f"Stack frame: {file}:{frame.line} in {frame.function}"]
    if cited is not None:
        switch_evidence.append(f"Code excerpt {cited[0]}:{cited[1]}: {cited[2]}")
    hypotheses = [
        {
            "title": fault.title,
            "description": fault.description,
            "confidence": fault.confidence,
            "evidence": evidence,
            "status": "hypothesis",
        },
        {
            "title": "The demo fault switch (DEMO_FAULTS_ENABLED) selects the unsafe branch",
            "description": (
                f"{fault.function} has a code path that only runs when the demo faults are "
                "enabled; the incident may be limited to environments with that switch on."
            ),
            "confidence": 0.3,
            "evidence": switch_evidence,
            "status": "hypothesis",
        },
    ]
    suspects = [
        {
            "file": file,
            "function": fault.function,
            "reason": f"Innermost application frame raising {fault.exception_type}",
        }
    ]
    callers = [f for f in demo_frames if f.function != fault.function]
    if callers:
        suspects.append(
            {
                "file": file,
                "function": callers[0].function,
                "reason": f"Calls {fault.function} (frame in the stack trace)",
            }
        )
    return {
        "summary": (
            f"[mock] Likely cause of {label}: {fault.title} (unconfirmed, confidence "
            f"{fault.confidence:.2f})."
        ),
        "hypotheses": hypotheses,
        "suspect_locations": suspects,
        "uncertainties": uncertainties,
        "next_steps": list(fault.next_steps),
    }


# -- patch ----------------------------------------------------------------------


def _read(reader: RepoReader, path: str) -> tuple[str, str]:
    """Return the file's text with ``\\n`` newlines and its original newline."""
    try:
        content = reader.read_file(path)
    except RepoAccessError as exc:
        raise NoPatchAvailable(f"cannot read {path} from the repository copy: {exc}") from exc
    if REDACTED in content:
        raise NoPatchAvailable(f"{path} contains redacted content; rewriting it is unsafe")
    newline = "\r\n" if "\r\n" in content else "\n"
    return content.replace("\r\n", "\n"), newline


def _restore_newlines(text: str, newline: str) -> str:
    return text.replace("\n", newline) if newline != "\n" else text


def replace_anchor(text: str, replacement: Replacement, path: str) -> str:
    """Replace the anchored snippet, which must appear exactly once."""
    count = text.count(replacement.anchor)
    first_line = replacement.anchor.strip().splitlines()[0]
    if count == 0:
        raise PatchAnchorNotFound(f"anchor not found in {path}: {first_line!r}")
    if count > 1:
        raise PatchAnchorNotFound(f"anchor is ambiguous ({count} matches) in {path}: {first_line!r}")
    return text.replace(replacement.anchor, replacement.replacement, 1)


def remove_tests(text: str, removed: tuple[RemovedTest, ...], path: str) -> str:
    """Remove top-level test functions; each must exist and contain its expectation."""
    if "fault_reproduction" not in text:
        raise PatchAnchorNotFound(f"{path} is not marked as fault_reproduction")
    for test in removed:
        pattern = re.compile(
            rf"^def {re.escape(test.name)}\(.*?(?=^\S|\Z)", re.MULTILINE | re.DOTALL
        )
        matches = list(pattern.finditer(text))
        if len(matches) != 1 or test.expects not in matches[0].group(0):
            raise PatchAnchorNotFound(f"anchor not found in {path}: def {test.name}(...)")
        text = text[: matches[0].start()] + text[matches[0].end() :]
    text = re.sub(r"\n{3,}", "\n\n\n", text)
    return text.rstrip("\n") + "\n"


def _patch_output(fault: DemoFault, prompt: Prompt) -> PatchModel:
    location = _suspect(fault, prompt)
    assert location is not None  # guaranteed by the rule's matcher
    root = prompt.context.get("repo_root")
    if not root:
        raise NoPatchAvailable("the repository copy is not available to read the current files")
    try:
        reader = RepoReader(str(root))
    except RepoAccessError as exc:
        raise NoPatchAvailable(str(exc)) from exc

    source_path = str(location["file"]).replace("\\", "/")
    prefix = source_path[: -len(DEMO_FILE)]
    source, newline = _read(reader, source_path)
    files = [
        FileChange(
            path=source_path,
            action=FileAction.MODIFY,
            content=_restore_newlines(replace_anchor(source, fault.fix, source_path), newline),
        )
    ]

    reproduction_path = prefix + REPRODUCTION_TESTS
    if (reader.root / reproduction_path).is_file():
        reproduction, newline = _read(reader, reproduction_path)
        updated = remove_tests(reproduction, fault.removed_tests, reproduction_path)
        files.append(
            FileChange(
                path=reproduction_path,
                action=FileAction.MODIFY,
                content=_restore_newlines(updated, newline),
            )
        )

    regression_path = prefix + fault.regression_test_path
    exists = (reader.root / regression_path).is_file()
    files.append(
        FileChange(
            path=regression_path,
            action=FileAction.MODIFY if exists else FileAction.CREATE,
            content=fault.regression_test,
        )
    )
    return PatchModel(files=files, explanation=fault.explanation)


# -- rule table -----------------------------------------------------------------


def _rules_for(fault: DemoFault) -> list[MockRule]:
    return [
        MockRule(
            name=f"demo-{fault.key}-rca",
            matches=lambda prompt, schema: _is_rca_request(prompt, schema)
            and _fault_frame(fault, prompt) is not None,
            respond=lambda prompt, schema: _rca_output(fault, prompt),
        ),
        MockRule(
            name=f"demo-{fault.key}-patch",
            matches=lambda prompt, schema: _is_patch_request(prompt, schema)
            and _suspect(fault, prompt) is not None,
            respond=lambda prompt, schema: _patch_output(fault, prompt),
        ),
    ]


def default_rules() -> list[MockRule]:
    """The mock provider's default rules: RCA and patch for each demo fault."""
    return [rule for fault in DEMO_FAULTS for rule in _rules_for(fault)]
