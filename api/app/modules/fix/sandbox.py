"""Runs tests against a throwaway copy of a service's source.

Two runners, chosen by FIX_SANDBOX:
  subprocess  the current Python in isolated mode (-I), scrubbed environment, its own temp/home folders,
              a timeout. No Docker needed. It isolates files (a copy is used) but not the network.
  docker      `docker run --network none` with memory/CPU/process limits and the copy mounted at /work.
              Needs FIX_SANDBOX_IMAGE (Python + pytest; see api/sandbox.Dockerfile).
Every run is a separate pytest process, so state one test changes (flags, globals) cannot leak into another.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import Settings
from app.modules.evidence.redaction import redact

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "dist",
             "build", "target", ".idea", ".vscode"}
MAX_FILES = 5_000
MAX_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_CHARS = 6_000
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")


class SandboxError(Exception):
    """The sandbox could not run (not the tests failing). Message is safe to show."""


@dataclass
class RunResult:
    name: str
    exit_code: int | None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    timed_out: bool = False
    duration_ms: int = 0
    output: str = ""

    @property
    def all_passed(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.errors == 0 and self.passed > 0

    @property
    def tests_failed(self) -> bool:
        """Tests ran and at least one failed (exit code 1), as opposed to a collection or usage error."""
        return self.exit_code == 1 and self.failed > 0

    def summary(self) -> str:
        if self.timed_out:
            return "timed out"
        parts = [f"{n} {k}" for n, k in ((self.passed, "passed"), (self.failed, "failed"), (self.errors, "error(s)"))
                 if n]
        return (", ".join(parts) or "no tests ran") + f" (exit {self.exit_code})"

    def to_dict(self) -> dict:
        return {**asdict(self), "summary": self.summary()}


def copy_source(source_root: str | Path, dest: Path) -> None:
    """Copy the source tree (without caches, VCS data or dependencies), refusing oversized trees."""
    src = Path(source_root)
    if not src.is_dir():
        raise SandboxError(f"source root {source_root} is not readable")
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(dirpath) / name
            files += 1
            size += path.stat().st_size
            if files > MAX_FILES or size > MAX_BYTES:
                raise SandboxError(f"source tree is larger than {MAX_FILES} files / {MAX_BYTES // 2**20} MB")
            shutil.copy2(path, dest / rel / name)


class Workspace:
    """A temporary copy of the source; files can be overwritten before running tests. Removed on exit."""

    def __init__(self, source_root: str | Path):
        self.root = Path(tempfile.mkdtemp(prefix="nexora-fix-"))
        try:
            copy_source(source_root, self.root / "src")
        except Exception:
            self.close()
            raise
        (self.root / "tmp").mkdir()
        (self.root / "home").mkdir()

    @property
    def src(self) -> Path:
        return self.root / "src"

    def write(self, rel_path: str, text: str) -> None:
        target = (self.src / rel_path).resolve()
        if self.src.resolve() not in target.parents:
            raise SandboxError(f"{rel_path} is outside the workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _pytest_args(targets: list[str], ignore: list[str]) -> list[str]:
    args = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "pythonpath=.", "--rootdir=.",
            "--tb=short", "--no-header", "-rfE"]
    args += [f"--ignore={p}" for p in ignore]
    return args + targets


def _scrubbed_env(ws: Workspace) -> dict[str, str]:
    """Only what Python needs to start; no keys, tokens or proxies from the parent environment."""
    env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8",
           "TMP": str(ws.root / "tmp"), "TEMP": str(ws.root / "tmp"), "TMPDIR": str(ws.root / "tmp"),
           "HOME": str(ws.root / "home"), "USERPROFILE": str(ws.root / "home")}
    for key in ("SYSTEMROOT", "WINDIR", "PATH", "LANG", "LC_ALL"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _result(name: str, proc_returncode: int | None, output: str, started: float, timed_out: bool) -> RunResult:
    counts: dict[str, int] = {}
    for n, kind in _COUNTS.findall(output):
        counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
    text = redact(output)
    if len(text) > MAX_OUTPUT_CHARS:
        text = "…(truncated)\n" + text[-MAX_OUTPUT_CHARS:]
    return RunResult(name=name, exit_code=proc_returncode, passed=counts.get("passed", 0),
                     failed=counts.get("failed", 0), errors=counts.get("error", 0), timed_out=timed_out,
                     duration_ms=int((time.perf_counter() - started) * 1000), output=text)


def run_pytest(ws: Workspace, settings: Settings, name: str, targets: list[str],
               ignore: list[str] | None = None) -> RunResult:
    ignore = ignore or []
    started = time.perf_counter()
    timeout = settings.fix_sandbox_timeout_seconds
    if settings.fix_sandbox == "docker":
        cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--cpus", "1",
               "--pids-limit", "128", "--security-opt", "no-new-privileges", "--read-only",
               "--tmpfs", "/tmp:rw,size=64m", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
               "-v", f"{ws.src}:/work", "-w", "/work", settings.fix_sandbox_image, "python", "-I",
               *_pytest_args(targets, ignore)]
        env, cwd = None, None
    elif settings.fix_sandbox == "subprocess":
        cmd = [sys.executable, "-I", *_pytest_args(targets, ignore)]
        env, cwd = _scrubbed_env(ws), ws.src
    else:
        raise SandboxError(f"unknown FIX_SANDBOX '{settings.fix_sandbox}' (use subprocess or docker)")
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return _result(name, None, out + f"\n[sandbox] killed after {timeout:g} s", started, True)
    except FileNotFoundError as exc:
        raise SandboxError(f"sandbox runner not found: {exc.filename}") from exc
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    if settings.fix_sandbox == "docker" and proc.returncode == 125:
        raise SandboxError(f"docker could not start the sandbox: {redact(proc.stderr.strip())[:300]}")
    return _result(name, proc.returncode, output, started, False)


def sandbox_status(settings: Settings) -> tuple[str, str]:
    """For the Checks page: can the configured sandbox run pytest? (status, detail)"""
    try:
        if settings.fix_sandbox == "docker":
            proc = subprocess.run(["docker", "image", "inspect", settings.fix_sandbox_image], capture_output=True,
                                  text=True, timeout=10, stdin=subprocess.DEVNULL)
            if proc.returncode != 0:
                return "failed", f"image {settings.fix_sandbox_image} not found (build api/sandbox.Dockerfile)"
            return "success", f"docker image {settings.fix_sandbox_image}, --network none"
        if settings.fix_sandbox != "subprocess":
            return "failed", f"unknown FIX_SANDBOX '{settings.fix_sandbox}'"
        proc = subprocess.run([sys.executable, "-I", "-m", "pytest", "--version"], capture_output=True, text=True,
                              timeout=20, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            return "failed", "pytest is not installed for the worker's Python"
        version = (proc.stdout or proc.stderr).strip().splitlines()[0]
        return "warning", f"subprocess ({version}); files isolated, network not isolated"
    except FileNotFoundError as exc:
        return "failed", f"{exc.filename} not found"
    except subprocess.TimeoutExpired:
        return "failed", "sandbox check timed out"
