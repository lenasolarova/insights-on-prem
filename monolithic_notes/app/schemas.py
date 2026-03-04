"""Pydantic schemas for API request/response validation."""
# This file defines the data shapes (schemas) for everything the API sends and receives.
# Pydantic models automatically validate input data types, serialize output to JSON,
# and generate OpenAPI documentation. FastAPI uses these as the contract for each endpoint.

# datetime: standard Python type for date+time values
from datetime import datetime
# Optional: marks a field as possibly None; List: typed list; Dict/Any: for flexible structures
from typing import Optional, List, Dict, Any
# BaseModel: base class for all Pydantic models — gives validation, serialization, and docs
# Field: used to attach metadata (description, default, alias) to model fields
from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    """Response schema for successful upload."""
    # Returned immediately after a file is accepted (HTTP 202).
    # The client can use request_id to correlate this upload with later log messages.

    # ... means this field is required (no default value)
    request_id: str = Field(..., description="Unique request identifier")
    # Processing status — will be "accepted" since actual processing runs in the background
    status: str = Field(..., description="Processing status")
    # The UTC timestamp when the upload was received by the server
    uploaded_at: datetime = Field(..., description="Upload timestamp")


class ErrorResponse(BaseModel):
    """Response schema for errors."""
    # Used by the global exception handler to ensure all errors have a consistent JSON shape.

    # Human-readable description of what went wrong
    error: str = Field(..., description="Error message")
    # The request ID from the HTTP header, if available — helps trace errors in logs
    request_id: Optional[str] = Field(None, description="Request ID if available")
    # Any extra context about the error (optional)
    detail: Optional[str] = Field(None, description="Additional error details")


# --- Schemas for the upgrade risks prediction endpoint ---

class UpgradeRisksPredictionRequest(BaseModel):
    """Request body for upgrade risks prediction."""
    # The client sends this JSON body when asking whether it's safe to upgrade a cluster.

    # UUID of the cluster to evaluate — used to query Thanos for that cluster's metrics
    cluster_id: str = Field(..., description="Cluster UUID")


class AlertResponse(BaseModel):
    """Alert information from Thanos metrics."""
    # Represents a single firing Prometheus alert associated with a cluster.

    # The Prometheus alert name (e.g. "KubeNodeNotReady")
    name: str = Field(..., description="Alert name")
    # The OpenShift namespace where this alert is firing (e.g. "openshift-etcd")
    namespace: Optional[str] = Field(None, description="Alert namespace")
    # Alert severity — either "warning" or "critical"
    severity: str = Field(..., description="Alert severity")
    # Direct link to this alert in the cluster's OpenShift console (built from console_url)
    url: Optional[str] = Field(None, description="Console URL for the alert")


class OperatorConditionResponse(BaseModel):
    """Failing operator condition from Thanos metrics."""
    # Represents a ClusterOperator that is in a degraded or unavailable state.

    # Name of the ClusterOperator (e.g. "etcd", "kube-apiserver")
    name: str = Field(..., description="Operator name")
    # The condition type that is failing: "Degraded" or "Not Available"
    condition: str = Field(..., description="Condition type")
    # Optional machine-readable reason code explaining why the condition is failing
    reason: Optional[str] = Field(None, description="Condition reason")
    # Direct link to this operator in the OpenShift console
    url: Optional[str] = Field(None, description="Console URL for the operator")


class UpgradeRisksPredictors(BaseModel):
    """Predictors that indicate upgrade risks."""
    # Groups together all detected risk factors for an upgrade prediction.

    # List of critical alerts that are considered blockers for upgrading
    alerts: List[AlertResponse] = Field(default_factory=list, description="Risky alerts")
    # List of cluster operators that are in failing conditions
    operator_conditions: List[OperatorConditionResponse] = Field(
        default_factory=list, description="Failing operator conditions"
    )


class UpgradeRisksPredictionResponse(BaseModel):
    """Response for upgrade risks prediction endpoint."""
    # Top-level response returned by the /upgrade-risks-prediction endpoint.

    # True if the cluster is considered safe to upgrade (no blocking alerts or conditions)
    upgrade_recommended: bool = Field(..., description="Whether upgrade is recommended")
    # All the specific risk factors found (alerts + operator conditions)
    upgrade_risks_predictors: UpgradeRisksPredictors = Field(
        ..., description="Detected upgrade risk predictors"
    )
    # Always "ok" if the request was processed without an internal server error
    status: str = Field(default="ok", description="Response status")


# --- Schemas for the v2 cluster report endpoint ---

class ReportMetaV2(BaseModel):
    """Metadata for v2 cluster report."""
    # Summary information about a cluster's report — sits at the top of a ReportV2 response.

    # The cluster UUID (used as both an identifier and a display name here)
    cluster_name: str = Field(..., description="Cluster UUID or name")
    # Whether this cluster is managed by ACM — always False in the on-prem context
    managed: bool = Field(default=False, description="Whether cluster is managed")
    # How many rules fired (had hits) in the latest report
    count: int = Field(..., description="Number of rules that hit")
    # ISO 8601 / RFC 3339 timestamp of when this cluster was last analyzed
    last_checked_at: Optional[str] = Field(None, description="Last check timestamp (RFC3339)")
    # ISO 8601 timestamp of when the original archive data was gathered on the cluster
    gathered_at: Optional[str] = Field(None, description="Data gathering timestamp (RFC3339)")


class RuleHitDetailedResponse(BaseModel):
    """Detailed rule hit response for v2 report (matching smart-proxy format)."""
    # Represents one rule that fired on the cluster — includes all the human-readable content.
    # The field names and structure match what insights-results-smart-proxy expects.

    # Fully qualified rule name (module path, e.g. "ccx_rules_ocp.external.rules.my_rule.report")
    rule_id: str = Field(..., description="Rule module/FQDN")
    # When this rule was first published (RFC 3339 string)
    created_at: Optional[str] = Field(None, description="When rule was created (RFC3339)")
    # Short human-readable description of what the rule checks
    description: str = Field(default="", description="Rule description")
    # Detailed explanation / generic content. Note: `alias="generic"` means the JSON field
    # name is "generic", but in Python code we access it as `details`.
    details: str = Field(default="", alias="generic", description="Generic/detailed information")
    # Why this rule fired on this cluster — the "reason" markdown content
    reason: str = Field(default="", description="Reason for the rule hit")
    # Steps the user can take to fix the problem — "resolution" markdown content
    resolution: str = Field(default="", description="Resolution steps")
    # Link to additional documentation or context for this rule
    more_info: str = Field(default="", description="Additional information URL")
    # Numeric risk level 1 (low) to 4 (critical) — used for sorting/filtering in the UI
    total_risk: int = Field(default=1, description="Total risk level (1-4)")
    # Whether the user has manually disabled/acknowledged this rule for their cluster
    disabled: bool = Field(default=False, description="Whether rule is disabled")
    # The user's justification text when they disabled this rule
    disable_feedback: str = Field(default="", description="Feedback for disabled rule")
    # When the user disabled this rule (empty string means it hasn't been disabled)
    disabled_at: Optional[str] = Field(default="", description="When rule was disabled")
    # Whether this rule is for internal Red Hat use only (hidden from external users)
    internal: bool = Field(default=False, description="Whether rule is internal")
    # User thumbs up/down vote: -1 (dislike), 0 (no vote), 1 (like)
    user_vote: int = Field(default=0, description="User vote (-1, 0, 1)")
    # Additional template data from the rule — rule-specific key/value pairs from insights-core
    extra_data: Dict[str, Any] = Field(default_factory=dict, description="Additional template data")
    # List of tags categorizing this rule (e.g. ["performance", "storage"])
    tags: List[str] = Field(default_factory=list, description="Rule tags")
    # When this cluster was first affected by this rule (RFC 3339 string), or None if unknown
    impacted: Optional[str] = Field(None, description="When cluster was impacted (RFC3339)")

    class Config:
        # populate_by_name=True allows this model to be instantiated using either the
        # Python attribute name ('details') or the JSON alias name ('generic').
        # This is needed because the alias is 'generic' but we construct the object with 'details'.
        populate_by_name = True  # Allow both 'details' and 'generic' field names


class ReportV2(BaseModel):
    """Report data structure for v2 endpoint."""
    # The full report for one cluster — metadata plus all rule hits.

    # Metadata about this report (cluster name, timestamps, count)
    meta: ReportMetaV2 = Field(..., description="Report metadata")
    # The list of rules that fired on this cluster, each with full content
    data: List[RuleHitDetailedResponse] = Field(default_factory=list, description="List of rule hits")


class ReportResponseV2(BaseModel):
    """Response schema for v2 cluster report endpoint."""
    # Top-level envelope returned by GET /api/v2/cluster/{id}/reports.

    # The actual report data
    report: ReportV2 = Field(..., description="Report data")
    # Always "ok" when the response is successful
    status: str = Field(default="ok", description="Response status")
