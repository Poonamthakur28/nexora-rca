"""Step 6: the subprocess sandbox runs pytest on a throwaway copy (real processes, tiny project)."""

import pytest

from app.modules.fix.sandbox import SandboxError, Workspace, copy_source, run_pytest


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "svc"
    (src / "app").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "app" / "calc.py").write_text("def half(x):\n    return x / 2\n")
    (src / "tests" / "test_calc.py").write_text("from app.calc import half\n\ndef test_half():\n    assert half(4) == 2\n")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("secret")
    return src


def test_passing_and_failing_runs_are_told_apart(project, settings):
    with Workspace(project) as ws:
        ok = run_pytest(ws, settings, "baseline", ["tests"])
        assert ok.all_passed and ok.passed == 1 and ok.exit_code == 0
        ws.write("tests/test_bad.py", "from app.calc import half\n\ndef test_zero():\n    half(None)\n")
        bad = run_pytest(ws, settings, "repro", ["tests/test_bad.py"])
    assert bad.tests_failed and not bad.all_passed and "TypeError" in bad.output
    assert bad.summary() == "1 failed (exit 1)"


def test_collection_errors_are_not_counted_as_reproduced(project, settings):
    with Workspace(project) as ws:
        ws.write("tests/test_bad.py", "import does_not_exist\n\ndef test_a():\n    pass\n")
        run = run_pytest(ws, settings, "repro", ["tests/test_bad.py"])
    assert not run.tests_failed and run.exit_code == 2


def test_timeout_stops_the_run(project, settings):
    settings.fix_sandbox_timeout_seconds = 3
    with Workspace(project) as ws:
        ws.write("tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
        run = run_pytest(ws, settings, "slow", ["tests/test_slow.py"])
    assert run.timed_out and run.summary() == "timed out" and not run.all_passed


def test_parent_secrets_do_not_reach_the_sandbox(project, settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    with Workspace(project) as ws:
        ws.write("tests/test_env.py", "import os\n\ndef test_env():\n"
                 "    assert 'ANTHROPIC_API_KEY' not in os.environ and 'GITHUB_TOKEN' not in os.environ\n")
        assert run_pytest(ws, settings, "env", ["tests/test_env.py"]).all_passed


def test_source_tree_is_copied_without_vcs_data_and_never_modified(project, settings):
    with Workspace(project) as ws:
        assert not (ws.src / ".git").exists() and (ws.src / "app" / "calc.py").is_file()
        ws.write("app/calc.py", "def half(x):\n    return 0\n")
        with pytest.raises(SandboxError, match="outside the workspace"):
            ws.write("../escape.py", "x = 1\n")
        root = ws.root
    assert not root.exists()  # removed on exit
    assert (project / "app" / "calc.py").read_text() == "def half(x):\n    return x / 2\n"


def test_missing_source_root_is_a_typed_error(tmp_path):
    with pytest.raises(SandboxError, match="not readable"):
        copy_source(tmp_path / "missing", tmp_path / "dest")


def test_checks_page_entry(settings):
    from app.modules.system_checks.routes import _sandbox_check

    check = _sandbox_check(settings)
    assert check.id == "fix-sandbox" and check.status == "warning" and "network not isolated" in check.detail
    settings.fix_sandbox = "docker"
    settings.fix_sandbox_image = "nexora-no-such-image:never"
    docker = _sandbox_check(settings)
    assert docker.status == "failed"
    settings.fix_enabled = False
    assert _sandbox_check(settings) is None


def test_unknown_runner_is_a_typed_error(project, settings):
    settings.fix_sandbox = "vm"
    with Workspace(project) as ws, pytest.raises(SandboxError, match="unknown FIX_SANDBOX"):
        run_pytest(ws, settings, "x", ["tests"])
