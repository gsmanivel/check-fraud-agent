from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict

Decision      = Literal["approve", "reject", "escalate"]
FraudPattern  = Literal["structuring", "altered_check", "synthetic_identity", "unknown"]
Engine        = Literal["native", "azure_agent"]


class ToolCall(BaseModel):
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class FraudDecision(BaseModel):
    decision: Decision
    risk_score: int = Field(..., ge=0, le=100)
    fraud_pattern: Optional[FraudPattern] = None
    fraud_indicators: List[str] = Field(default_factory=list)
    reasoning: str = ""
    tool_calls_made: List[ToolCall] = Field(default_factory=list)
    iterations: int = 0
    engine: Engine

    model_config = ConfigDict(extra="ignore")


class FraudDecisionLLMOutput(BaseModel):
    """Strict-mode JSON Schema passed to Azure OpenAI as response_format.

    No defaults, every field required, additionalProperties=false, no
    numeric range constraints — OpenAI strict mode rejects `minimum`/`maximum`.
    Range checks live on the wrapping FraudDecision.
    """
    decision: Decision
    risk_score: int
    fraud_pattern: Optional[FraudPattern]
    fraud_indicators: List[str]
    reasoning: str

    model_config = ConfigDict(extra="forbid")

class ExtractedFields(BaseModel):
    micr_valid: bool = True
    amount_mismatch: bool = False
    payee_match: bool = True
    alteration_detected: bool = False
    signature_present: bool = True
    raw_ocr_confidence: float = Field(1.0, ge=0.0, le=1.0)

class CheckPayload(BaseModel):
    id: str
    check_number: Optional[str] = None
    account_number: str
    routing_number: Optional[str] = None
    customer_name: Optional[str] = None
    payee_name: Optional[str] = None
    amount: float
    memo: Optional[str] = None
    issue_date: Optional[str] = None
    submission_date: Optional[str] = None
    bank_name: Optional[str] = None
    blob_path: Optional[str] = None
    check_image_url: Optional[str] = None
    status: Optional[str] = None
    extracted_fields: Optional[ExtractedFields] = Field(default_factory=ExtractedFields)
    
    # Internal states
    processing_tier: Optional[str] = None
    fraud_decision: Optional[str] = None
    risk_score: Optional[int] = None
    fraud_indicators: List[str] = Field(default_factory=list)
    tier1_risk_score: Optional[int] = None
    tier1_indicators: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")
