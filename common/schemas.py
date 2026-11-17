"""模拟系统的数据契约；区标签是元数据，不是授权凭证。"""
from __future__ import annotations
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, Field


class Zone(str, Enum):
    GREEN = "green"
    RED = "red"
    UNKNOWN = "unknown"


class SourceType(str, Enum):
    PUBLIC_CLASSIC = "public_classic"
    FAQ = "faq"
    APPROVED = "approved"
    ORIGINAL = "original"


class Provenance(BaseModel):
    source_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    locator: str = ""
    source_type: SourceType = SourceType.PUBLIC_CLASSIC
    quote: str = ""
    version: str = "simulation-v1"
    content_hash: str = ""


class TaskEnvelope(BaseModel):
    task_id: str = Field(default="", max_length=128, pattern=r"^[a-zA-Z0-9_-]*$")
    query: str = Field(min_length=1, max_length=2000)
    zone: Zone = Zone.UNKNOWN
    gate_approved: bool = False  # 仅兼容旧请求；入口不将此值作为授权。
    user_id: str = Field(default="anon", max_length=64)
    top_k: int = Field(default=4, ge=1, le=20)
    max_iterations: int = Field(default=3, ge=1, le=6)
    budget_cny: Decimal = Field(default=Decimal("0.50"), ge=0, le=Decimal("50.00"), allow_inf_nan=False)


class Retrieved(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    provenance: Provenance
    zone: Zone = Zone.UNKNOWN  # 缺失区标签不能默认为公开。


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=20)


class Candidate(BaseModel):
    task_id: str
    answer: str = Field(max_length=16000)
    evidence: list[Retrieved] = Field(default_factory=list, max_length=20)
    model: str = "unknown"
    from_zone: Zone = Zone.UNKNOWN


class InferRequest(BaseModel):
    task_id: str
    query: str = Field(min_length=1, max_length=2000)
    evidence: list[Retrieved] = Field(default_factory=list, max_length=20)
    zone: Zone = Zone.UNKNOWN


class VerifyStatus(str, Enum):
    PASS = "pass"
    REVISE = "revise"
    REJECT = "reject"


class VerificationResult(BaseModel):
    task_id: str
    status: VerifyStatus
    hallucination_risk: float = Field(default=0.0, ge=0, le=1)
    provenance_ok: bool = False
    compliance_ok: bool = False
    reasons: list[str] = Field(default_factory=list)


class Answer(BaseModel):
    task_id: str
    answer: str
    evidence: list[Provenance] = Field(default_factory=list)
    verification: VerificationResult
    refused: bool = False
    note: str = ""
    model: str = "none"
    trace: list[str] = Field(default_factory=list)
    simulated: bool = True
