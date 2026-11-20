"""Where a verified fix goes: patch files on disk (always) and a draft pull request (when configured).

Nothing here merges anything. The draft PR is refused when the remote copy of a changed file differs
from the local file the patch was verified against, so a PR never overwrites someone else's change.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.config import Settings

API_ROOT = Path(__file__).resolve().parents[3]  # .../api
GITHUB_TIMEOUT_SECONDS = 20.0
_GITHUB_REPO = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")


def artifacts_root(settings: Settings) -> Path:
    return Path(settings.fix_artifacts_dir) if settings.fix_artifacts_dir else API_ROOT / "var" / "fixes"


def write_artifacts(settings: Settings, folder: str, files: dict[str, str]) -> Path:
    """Write the patch, test, sandbox log and PR text. Returns the folder (git-ignored by default)."""
    out = artifacts_root(settings) / folder
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    return out


@dataclass
class PrError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class PrRequest:
    repository_url: str
    repository_path: str  # folder of the service inside the repository ("" = repository root)
    base_branch: str
    branch: str
    title: str
    body: str
    originals: dict[str, str]  # service-relative path -> text the patch was verified against
    changes: dict[str, str]  # service-relative path -> new text (application files and the new test)


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


class GitHubDraftPr:
    """Opens a draft PR through the GitHub REST API (blobs -> tree -> commit -> branch -> pull)."""

    def __init__(self, settings: Settings, http: httpx.Client):
        self.settings = settings
        self.http = http

    def _call(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.settings.github_api_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {self.settings.github_token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        try:
            r = self.http.request(method, url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS, **kwargs)
        except httpx.TimeoutException as exc:
            raise PrError("timeout", f"GitHub did not answer within {GITHUB_TIMEOUT_SECONDS:g} s") from exc
        except httpx.HTTPError as exc:
            raise PrError("unreachable", f"GitHub request failed: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            detail = r.text[:200].replace("\n", " ")
            raise PrError(f"http_{r.status_code}", f"{method} {path} returned {r.status_code}: {detail}")
        return r.json() if r.content else {}

    def open(self, req: PrRequest) -> str:
        if not self.settings.github_token:
            raise PrError("not_configured", "GITHUB_TOKEN is not set")
        m = _GITHUB_REPO.match(req.repository_url or "")
        if not m:
            raise PrError("unsupported_repository", f"repository_url {req.repository_url!r} is not a GitHub repository")
        repo = f"/repos/{m['owner']}/{m['repo']}"
        prefix = req.repository_path.strip("/")
        full = lambda p: f"{prefix}/{p}" if prefix else p  # noqa: E731

        base_sha = self._call("GET", f"{repo}/git/ref/heads/{req.base_branch}")["object"]["sha"]
        base_tree = self._call("GET", f"{repo}/git/commits/{base_sha}")["tree"]["sha"]
        for path, local in req.originals.items():
            remote = self._call("GET", f"{repo}/contents/{full(path)}", params={"ref": req.base_branch})
            text = base64.b64decode(remote.get("content", "")).decode("utf-8", errors="replace")
            if _lf(text) != _lf(local):
                raise PrError("base_mismatch", f"{full(path)} on {req.base_branch} differs from the verified local "
                                               "file; push the base first or apply the patch files by hand")
        tree = []
        for path, text in req.changes.items():
            blob = self._call("POST", f"{repo}/git/blobs", json={"content": _lf(text), "encoding": "utf-8"})
            tree.append({"path": full(path), "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self._call("POST", f"{repo}/git/trees", json={"base_tree": base_tree, "tree": tree})["sha"]
        commit = self._call("POST", f"{repo}/git/commits",
                            json={"message": req.title, "tree": new_tree, "parents": [base_sha]})["sha"]
        self._call("POST", f"{repo}/git/refs", json={"ref": f"refs/heads/{req.branch}", "sha": commit})
        pr = self._call("POST", f"{repo}/pulls", json={"title": req.title, "head": req.branch,
                                                        "base": req.base_branch, "body": req.body, "draft": True})
        return pr["html_url"]
