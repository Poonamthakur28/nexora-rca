"""The RCA report every provider must return. Validated strictly; anything else is rejected."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["code", "dependency", "resource", "configuration", "infrastructure", "capacity", "unknown"]
ACTIONS = ("rollback", "disable_feature", "scale_out", "restart", "failover", "config_change", "wait", "other")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RootCause(_Strict):
    category: Category
    component: str = Field(max_length=200)
    file: str | None = Field(default=None, max_length=300)
    line: int | None = Field(default=None, ge=1)
    function: str | None = Field(default=None, max_length=200)
    explanation: str = Field(max_length=2000)


class Trigger(_Strict):
    kind: Literal["release", "traffic", "external", "unknown"]
    release_version: str | None = Field(default=None, max_length=50)
    explanation: str = Field(default="", max_length=1000)


class Hypothesis(_Strict):
    title: str = Field(max_length=300)
    likelihood: float = Field(ge=0, le=1)
    status: Literal["accepted", "rejected", "open"]
    supporting: list[str] = Field(default_factory=list, max_length=20)
    contradicting: list[str] = Field(default_factory=list, max_length=20)
    reasoning: str = Field(default="", max_length=1000)


class MitigationOption(_Strict):
    action: Literal[ACTIONS]  # type: ignore[valid-type]
    target: str = Field(max_length=200)
    reason: str = Field(max_length=600)
    risk: Literal["low", "medium", "high"]


class RcaReportModel(_Strict):
    summary: str = Field(max_length=400)
    root_cause: RootCause
    trigger: Trigger
    symptom_vs_cause: str = Field(default="", max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=8)
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)
    next_checks: list[str] = Field(default_factory=list, max_length=10)
    mitigation_options: list[MitigationOption] = Field(default_factory=list, max_length=6)

    @field_validator("evidence_refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return [r.strip() for r in v if r and r.strip()]


SCHEMA_TEXT = """{
  "summary": "one sentence a responder reads first",
  "root_cause": {
    "category": "code | dependency | resource | configuration | infrastructure | capacity | unknown",
    "component": "the service, module or downstream system at fault",
    "file": "repo-relative path, or null if the cause is not in our code",
    "line": 123,
    "function": "function name or null",
    "explanation": "what is wrong and why it produces the observed failure"
  },
  "trigger": {"kind": "release | traffic | external | unknown", "release_version": "v1.2.0 or null",
              "explanation": "what started it"},
  "symptom_vs_cause": "where the error surfaces vs where the defect is, if they differ",
  "confidence": 0.0,
  "evidence_refs": ["E1", "E3"],
  "hypotheses": [{"title": "...", "likelihood": 0.0, "status": "accepted | rejected | open",
                  "supporting": ["E1"], "contradicting": ["E4"], "reasoning": "..."}],
  "missing_evidence": ["what would raise confidence"],
  "next_checks": ["concrete things a responder should verify"],
  "mitigation_options": [{"action": "rollback | disable_feature | scale_out | restart | failover | config_change | wait | other",
                          "target": "...", "reason": "...", "risk": "low | medium | high"}]
}"""
