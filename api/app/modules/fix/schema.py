"""The two answers a fix provider must return. Validated strictly; anything else is rejected."""

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ReproTest(_Strict):
    test_code: str = Field(min_length=20, max_length=20_000)
    expected_exception: str | None = Field(default=None, max_length=100)
    explanation: str = Field(default="", max_length=1500)


class PatchProposal(_Strict):
    diff: str = Field(min_length=20, max_length=20_000)
    explanation: str = Field(default="", max_length=1500)


REPRO_SHAPE = '{"test_code": "import pytest\\n...", "expected_exception": "AttributeError or null", "explanation": "..."}'
PATCH_SHAPE = '{"diff": "--- a/app/x.py\\n+++ b/app/x.py\\n@@ ... @@\\n ...", "explanation": "..."}'
