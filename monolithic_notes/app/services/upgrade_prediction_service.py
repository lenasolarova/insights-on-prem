"""Service for predicting upgrade risks based on cluster metrics."""
# This module contains the business logic that decides whether a cluster is safe to upgrade.
# It applies static filtering rules to the raw alerts and operator conditions fetched
# from Thanos/Prometheus, then formats the result for the API response.

# logging: standard Python library for log messages
import logging
# List: type hint for a list of a specific type
from typing import List
# urljoin: safely combines a base URL with a relative path to form a full URL
from urllib.parse import urljoin

# Import Pydantic response schemas used to build the API response objects
from app.schemas import (
    AlertResponse,                   # Schema for a single alert in the response
    OperatorConditionResponse,       # Schema for a single operator condition in the response
    UpgradeRisksPredictors,          # Schema that groups alerts + operator conditions
    UpgradeRisksPredictionResponse,  # Top-level response schema for the endpoint
)
# Import the internal data classes produced by the ThanosService
from app.services.thanos_service import Alert, OperatorCondition

# Set up a module-level logger
logger = logging.getLogger(__name__)

# List of OpenShift namespaces whose alerts are intentionally excluded from upgrade risk analysis.
# These namespaces contain optional or add-on operators (CNV, logging, pipelines, etc.)
# that may fire alerts for reasons unrelated to the core cluster health, and should
# not block an upgrade recommendation.
EXCLUDE_NAMESPACES = [
    "openshift-cnv",                    # OpenShift Virtualization (KubeVirt add-on)
    "openshift-compliance",             # Compliance Operator (security scanning)
    "openshift-operators",              # General operator hub namespace
    "openshift-storage",                # OpenShift Data Foundation (storage add-on)
    "openshift-logging",                # OpenShift Logging (log aggregation add-on)
    "openshift-gitops",                 # OpenShift GitOps / ArgoCD (CI/CD add-on)
    "openshift-pipelines",              # OpenShift Pipelines / Tekton (CI/CD add-on)
    "openshift-image-registry",         # Internal container image registry
    "openshift-marketplace",            # OperatorHub marketplace
    "openshift-redhat-marketplace",     # Red Hat Marketplace operator
    "openshift-distributed-tracing",    # Distributed tracing (Jaeger) add-on
    "openshift-gitlab-runner",          # GitLab Runner operator
    "openshift-devspaces",              # OpenShift Dev Spaces (cloud IDE)
    "openshift-logs",                   # Additional logging namespace
]


class UpgradePredictionService:
    """Static rule-based predictor for upgrade risks."""

    def _filter_alert(self, alert: Alert) -> bool:
        # Determine whether a single alert should count as an upgrade blocker.
        # Returns True if the alert is a real risk, False if it should be ignored.

        # Alerts with no namespace are not namespace-scoped (e.g. node-level alerts)
        # and are excluded from the upgrade risk check.
        if alert.namespace is None:
            return False

        # An alert is considered a risk only if ALL of the following are true:
        return (
            alert.severity == "critical"               # Must be "critical" severity (not just "warning")
            and alert.namespace.startswith("openshift-")  # Must be in an openshift-* namespace
            and alert.namespace not in EXCLUDE_NAMESPACES  # Must not be an excluded add-on namespace
        )

    def _filter_foc(self, foc: OperatorCondition) -> bool:
        # Determine whether a ClusterOperator condition should be considered an upgrade blocker.
        # Returns True if the condition indicates a real problem.

        # Only "Not Available" and "Degraded" are considered blockers.
        # Operators in these states must be healthy before an upgrade is safe.
        return foc.condition in ["Not Available", "Degraded"]

    def _build_alert_url(self, console_url: str, alert_name: str) -> str:
        # Construct a deep link into the OpenShift console's Alerts page, pre-filtered
        # to show the specific alert by name. Returns empty string if either input is missing.
        if not console_url or not alert_name:
            return ""
        # urljoin safely appends the relative path to the base console URL.
        # The query string filters the Monitoring Alerts view to this specific alert.
        return urljoin(
            console_url,
            f"/monitoring/alerts?orderBy=asc&sortBy=Severity&alert-name={alert_name}",
        )

    def _build_foc_url(self, console_url: str, operator_name: str) -> str:
        # Construct a deep link into the OpenShift console's ClusterOperator detail page
        # for a specific operator. Returns empty string if either input is missing.
        if not console_url or not operator_name:
            return ""
        # This URL navigates to the Kubernetes resource page for the named ClusterOperator.
        # config.openshift.io~v1~ClusterOperator is the API group/version/kind path format
        # used by the OpenShift console's resource browser.
        return urljoin(
            console_url,
            f"/k8s/cluster/config.openshift.io~v1~ClusterOperator/{operator_name}",
        )

    def predict(
        self,
        alerts: List[Alert],                       # Raw alerts from Thanos
        operator_conditions: List[OperatorCondition],  # Raw operator conditions from Thanos
        console_url: str,                          # Base URL of the cluster's console
    ) -> UpgradeRisksPredictionResponse:
        """Filter alerts and FOCs to identify actual upgrade risks."""

        # Apply the alert filter — keep only alerts that meet the risk criteria
        filtered_alerts = [a for a in alerts if self._filter_alert(a)]
        # Apply the operator condition filter — keep only Degraded / Not Available operators
        filtered_focs = [f for f in operator_conditions if self._filter_foc(f)]

        # Business rule: fewer than 2 critical alerts is not enough to flag as a blocker.
        # A single isolated alert may be transient noise; 2+ suggests a real cluster problem.
        if len(filtered_alerts) < 2:
            filtered_alerts = []  # Clear the list — not enough alerts to matter

        # Convert internal Alert dataclass instances into Pydantic AlertResponse objects
        # suitable for inclusion in the API response. Attach a console deep-link URL to each.
        alert_responses = [
            AlertResponse(
                name=a.name,
                namespace=a.namespace,
                severity=a.severity,
                # Build the console deep link; use None if the URL is empty (falsy)
                url=self._build_alert_url(console_url, a.name) or None,
            )
            for a in filtered_alerts
        ]

        # Convert internal OperatorCondition instances into Pydantic OperatorConditionResponse objects
        foc_responses = [
            OperatorConditionResponse(
                name=f.name,
                condition=f.condition,
                reason=f.reason,
                # Build the console deep link; use None if the URL is empty (falsy)
                url=self._build_foc_url(console_url, f.name) or None,
            )
            for f in filtered_focs
        ]

        # The upgrade is only recommended (safe) if there are NO risky alerts AND
        # NO failing operator conditions. Any blocker means upgrade_recommended=False.
        upgrade_recommended = len(alert_responses) == 0 and len(foc_responses) == 0

        # Build and return the final API response object
        return UpgradeRisksPredictionResponse(
            upgrade_recommended=upgrade_recommended,
            upgrade_risks_predictors=UpgradeRisksPredictors(
                alerts=alert_responses,
                operator_conditions=foc_responses,
            ),
        )
