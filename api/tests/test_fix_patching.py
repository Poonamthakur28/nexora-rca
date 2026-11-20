"""Step 6: plain-code checks on what the fix provider writes (no provider, no sandbox)."""

import pytest

from app.modules.fix.patching import (
    PatchError,
    apply_file_diff,
    canonical_diff,
    check_test_safety,
    parse_unified_diff,
    validate_scope,
)

PRICING = (
    "def find_tax_rule(region):\n"
    "    rule = TAX_RULES.get(region)\n"
    "    if rule is None and not is_enabled(\"code_null_tax\"):\n"
    "        return TaxRule(region, 0.18)\n"
    "    return rule\n"
)

FIX = """--- a/app/pricing.py
+++ b/app/pricing.py
@@ -40,3 +40,3 @@
     rule = TAX_RULES.get(region)
-    if rule is None and not is_enabled("code_null_tax"):
+    if rule is None:
         return TaxRule(region, 0.18)
"""
ALLOWED = {"app/pricing.py"}


# ---------- diff parsing and applying ----------

def test_hunk_is_located_by_context_even_with_wrong_header_numbers():
    (diff,) = parse_unified_diff(FIX)
    assert diff.path == "app/pricing.py" and diff.changed_lines() == 2
    assert "    if rule is None:\n" in apply_file_diff(PRICING, diff)


def test_windows_line_endings_and_final_newline_are_kept():
    (diff,) = parse_unified_diff(FIX)
    out = apply_file_diff(PRICING.replace("\n", "\r\n"), diff)
    assert out.endswith("return rule\r\n") and "\r\n    if rule is None:\r\n" in out and "\n\n" not in out


def test_fenced_diff_is_tolerated():
    assert parse_unified_diff("```diff\n" + FIX + "```")[0].path == "app/pricing.py"


def test_context_that_does_not_match_is_rejected():
    (diff,) = parse_unified_diff(FIX.replace("TAX_RULES.get", "RULES.lookup"))
    with pytest.raises(PatchError, match="context does not match"):
        apply_file_diff(PRICING, diff)


def test_ambiguous_context_is_rejected_not_guessed():
    text = "x = 1\nx = 1\n"
    (diff,) = parse_unified_diff("--- a/app/a.py\n+++ b/app/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    with pytest.raises(PatchError, match="matches 2 places"):
        apply_file_diff(text, diff)


def test_pure_insertion_without_context_is_rejected():
    (diff,) = parse_unified_diff("--- a/app/a.py\n+++ b/app/a.py\n@@ -0,0 +1 @@\n+import os\n")
    with pytest.raises(PatchError, match="no context lines"):
        apply_file_diff("x = 1\n", diff)


@pytest.mark.parametrize("text", ["", "just words", "--- a/x\n+++ b/x\n@@ nonsense @@\n"])
def test_malformed_diffs_are_rejected(text):
    with pytest.raises(PatchError):
        parse_unified_diff(text)


def test_canonical_diff_is_clean_and_marks_new_files():
    (diff,) = parse_unified_diff(FIX)
    out = canonical_diff("app/pricing.py", PRICING.replace("\n", "\r\n"), apply_file_diff(PRICING, diff))
    assert out.startswith("--- a/app/pricing.py\n+++ b/app/pricing.py\n@@ -1,5 +1,5 @@") and "\r" not in out
    assert canonical_diff("tests/test_x.py", "", "def test_a():\n    pass\n").startswith("--- /dev/null\n+++ b/tests/test_x.py")


# ---------- scope ----------

def _scope(diff_text, allowed=ALLOWED, max_lines=40):
    return validate_scope(parse_unified_diff(diff_text), allowed, max_lines)


def test_patch_within_scope_passes():
    assert _scope(FIX) == []


def test_changing_tests_is_rejected():
    text = "--- a/tests/test_baseline.py\n+++ b/tests/test_baseline.py\n@@ -1 +1 @@\n-assert x\n+assert True\n"
    assert any("tests may not be changed" in p for p in _scope(text, ALLOWED | {"tests/test_baseline.py"}))


def test_files_outside_the_rca_are_rejected():
    text = '--- a/app/bugs.py\n+++ b/app/bugs.py\n@@ -1 +1 @@\n-"enabled": False,\n+"enabled": False,\n'
    assert any("not one of the files" in p for p in _scope(text))


@pytest.mark.parametrize("header,expected", [
    ("--- /dev/null\n+++ b/app/new.py", "creating or deleting"),
    ("--- a/app/pricing.py\n+++ /dev/null", "creating or deleting"),
    ("--- a/app/pricing.py\n+++ b/app/other.py", "renaming"),
    ("--- a/../secrets.py\n+++ b/../secrets.py", "stay inside"),
    ("--- a/C:/Windows/x.py\n+++ b/C:/Windows/x.py", "stay inside"),
])
def test_creating_renaming_and_escaping_paths_are_rejected(header, expected):
    problems = _scope(f"{header}\n@@ -1 +1 @@\n-a\n+b\n")
    assert any(expected in p for p in problems), problems


def test_size_limit():
    body = "".join(f"-line {i}\n+line {i}!\n" for i in range(30))
    problems = _scope(f"--- a/app/pricing.py\n+++ b/app/pricing.py\n@@ -1,30 +1,30 @@\n{body}")
    assert any("changes 60 lines; the limit is 40" in p for p in problems)


# ---------- reproduction test safety ----------

GOOD_TEST = """from app import bugs
from app.pricing import calculate_total

def test_unknown_region():
    bugs.set_enabled("code_null_tax", True)
    try:
        assert calculate_total(100.0, "TN")["total"] == 118.0
    finally:
        bugs.set_enabled("code_null_tax", False)
"""


def test_plain_test_is_accepted():
    assert check_test_safety(GOOD_TEST) == []


@pytest.mark.parametrize("code,expected", [
    ("import subprocess\ndef test_a():\n    subprocess.run(['x'])\n", "imports subprocess"),
    ("from urllib.request import urlopen\ndef test_a():\n    urlopen('http://x')\n", "imports from urllib.request"),
    ("import os\ndef test_a():\n    os.system('rm -rf /')\n", "calls os.system()"),
    ("def test_a():\n    eval('1')\n", "calls eval()"),
    ("def helper():\n    pass\n", "no test function"),
    ("def test_a(:\n", "does not parse"),
])
def test_unsafe_or_invalid_tests_are_rejected(code, expected):
    assert any(expected in p for p in check_test_safety(code))
