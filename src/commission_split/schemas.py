"""Pydantic schemas for commission split pipeline."""

from pydantic import BaseModel, Field


class ProductMatch(BaseModel):
    product_name: str = Field(..., description="Matched product name from Excel")
    product_id: int = Field(..., description="Product ID; -1 when no match found")
    rule: str = Field(..., description="Raw rule text from product Excel; empty when no match")
    matched: bool = Field(True, description="False when product name has no match in catalog")


class RuleLevel(BaseModel):
    level_index: int = Field(..., description="0=agent, 1=reporting manager, 2=manager's manager, etc. -1=agency")
    label: str = Field(..., description="Human-readable role label")
    percentage: float = Field(..., description="Percentage of commission (0-100)")


class ParsedRule(BaseModel):
    levels: list[RuleLevel] = Field(..., description="Commission levels ordered from agent (index 0) upward")
    reasoning: str = Field("", description="Short explanation of how the rule was interpreted")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Confidence in the parse, 0.0–1.0")


class HierarchyMember(BaseModel):
    agent_id: int | None = Field(None, description="Agent ID; None for AGENCY")
    name: str = Field(..., description="Agent name or AGENCY")
    level_index: int = Field(..., description="0=agent, 1=manager, -1=agency, -2=subagent")
    is_subagent: bool = Field(False, description="True when this member is below the rule's lowest level")
    subagent_of: int | None = Field(None, description="agent_id of the member whose 50% this subagent receives")


class AgentHierarchy(BaseModel):
    members: list[HierarchyMember] = Field(
        ...,
        description="Hierarchy from AGENCY (top) down to selling agent (bottom)",
    )


class CommissionSplit(BaseModel):
    name: str
    level_index: int
    percentage: float
    amount: float = Field(..., description="Commission amount in dollars")


class CommissionResult(BaseModel):
    product: str
    policy_number: str
    total_commission: float
    splits: list[CommissionSplit] = Field(..., description="Split per hierarchy level, top (AGENCY) first")
    rule_reasoning: str = Field("", description="LLM explanation of how the rule was interpreted")
    rule_confidence: float = Field(1.0, ge=0.0, le=1.0, description="LLM confidence in the rule parse")
