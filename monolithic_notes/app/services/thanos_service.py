"""Service for querying Thanos/Prometheus metrics via rbac-query-proxy."""
# Thanos is a highly available Prometheus setup used in OpenShift's monitoring stack.
# This service uses the rbac-query-proxy (an in-cluster HTTP endpoint) to fetch
# live metrics for managed clusters — specifically: alerts, operator conditions,
# and the cluster's console URL.

# logging: standard Python library for emitting structured log messages
import logging
# datetime, timedelta: standard types for working with timestamps and time offsets
from datetime import datetime, timedelta
# dataclass, field: decorator + helper for defining simple data-holding classes
from dataclasses import dataclass, field
# List, Optional, Tuple: type annotations for return types and parameters
from typing import List, Optional, Tuple

# httpx: a modern HTTP client library (similar to `requests`) with async support
# Used here to make synchronous HTTP GET requests to the Thanos query API
import httpx

# AppConfig: the application configuration dataclass (holds thanos_url, timeout, etc.)
from app.config import AppConfig

# Set up a module-level logger. Log messages from this module will be prefixed with
# the module path (e.g. "app.services.thanos_service")
logger = logging.getLogger(__name__)

# Path inside the container where Kubernetes automatically mounts the service account token.
# This token is used as a Bearer token to authenticate to the rbac-query-proxy.
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

# Path to the service account's CA certificate bundle.
# Used to verify the TLS certificate presented by the rbac-query-proxy (in-cluster TLS).
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"


# A lightweight data class to hold information about a single firing Prometheus alert.
# @dataclass auto-generates __init__, __repr__, and __eq__ from the annotated fields.
@dataclass
class Alert:
    """Alert extracted from Prometheus metrics."""

    # The alertname label from Prometheus (e.g. "KubeNodeNotReady")
    name: str
    # The namespace where this alert is firing (e.g. "openshift-etcd"), or None if not namespaced
    namespace: Optional[str] = None
    # Alert severity label — "warning" or "critical"
    severity: str = ""


# A lightweight data class for a ClusterOperator that is in a failing condition.
@dataclass
class OperatorCondition:
    """Failing operator condition extracted from Prometheus metrics."""

    # Name of the ClusterOperator resource (e.g. "etcd", "authentication")
    name: str
    # Which condition type is failing: "Degraded" or "Not Available"
    # (Note: "Available == 0" in Prometheus is translated to "Not Available" here)
    condition: str
    # Optional reason code from the operator's status (e.g. "AsExpected", "Error")
    reason: Optional[str] = None


class ThanosService:
    """Queries Thanos for cluster health metrics."""

    def __init__(self, config: AppConfig):
        # Store the Thanos endpoint URL from config (e.g. https://rbac-query-proxy.../...)
        self.thanos_url = config.thanos_url
        # Max seconds to wait for a Thanos HTTP response before timing out
        self.timeout = config.thanos_query_timeout
        # How many minutes in the past to evaluate the PromQL query at
        # (e.g. 60 means: give me the state of these metrics 60 minutes ago)
        self.lookback_minutes = config.thanos_query_lookback_minutes

    def _get_bearer_token(self) -> str:
        # Open and read the Kubernetes service account token file.
        # Kubernetes automatically rotates and mounts this token at the known path.
        # .strip() removes any trailing newline characters.
        with open(SA_TOKEN_PATH) as f:
            return f.read().strip()

    def _build_query(self, cluster_id: str) -> str:
        # Build a PromQL (Prometheus Query Language) expression that retrieves four kinds of data
        # for a specific cluster in a single HTTP call, joined with `or` (union of all results).
        #
        # 1. console_url{clusterID=~"<id>"}
        #    A metric whose labels contain the cluster's console URL — used to build deep links.
        #
        # 2. ALERTS{clusterID=~"<id>", namespace=~"openshift-.*", severity=~"warning|critical"}
        #    Active Prometheus alerts for OpenShift namespaces matching the given cluster.
        #
        # 3. cluster_operator_conditions{..., condition="Available"} == 0
        #    ClusterOperators where Available == 0 (i.e. the operator is NOT available).
        #
        # 4. cluster_operator_conditions{..., condition="Degraded"} == 1
        #    ClusterOperators where Degraded == 1 (i.e. the operator IS degraded).
        return (
            f'console_url{{clusterID=~"{cluster_id}"}}'
            " or "
            f'ALERTS{{clusterID=~"{cluster_id}", namespace=~"openshift-.*", severity=~"warning|critical"}}'
            " or "
            f'cluster_operator_conditions{{clusterID=~"{cluster_id}", condition="Available"}} == 0'
            " or "
            f'cluster_operator_conditions{{clusterID=~"{cluster_id}", condition="Degraded"}} == 1'
        )

    def _parse_response(
        self, data: dict
    ) -> Tuple[str, List[Alert], List[OperatorCondition]]:
        # Parse the JSON body returned by Thanos's /api/v1/query endpoint.
        # The response contains a list of metric time-series results; we classify each
        # by the __name__ label (the metric name) and extract the relevant fields.

        # Default values if the data doesn't contain the expected fields
        console_url = ""
        alerts: List[Alert] = []
        operator_conditions: List[OperatorCondition] = []

        # The Thanos response structure is: {"data": {"result": [...]}}
        # Each element in "result" is a metric series: {"metric": {...labels...}, "value": [...]}
        results = data.get("data", {}).get("result", [])

        for result in results:
            # "metric" holds the label set for this time series (e.g. {__name__: "ALERTS", alertname: "..."})
            metric = result.get("metric")
            if not metric:
                # Skip any results that have no labels (malformed or empty)
                continue

            # __name__ is the special Prometheus label that holds the metric name
            name = metric.get("__name__")

            if name == "console_url":
                # This metric's "url" label holds the full URL of the cluster console
                url = metric.get("url")
                if url:
                    console_url = url  # Save it for building deep links later

            elif name == "ALERTS":
                # This is a firing Prometheus alert — extract the relevant labels
                alerts.append(
                    Alert(
                        name=metric.get("alertname", ""),    # The human-readable alert name
                        namespace=metric.get("namespace"),   # Namespace the alert originates from
                        severity=metric.get("severity", ""), # "warning" or "critical"
                    )
                )

            elif name == "cluster_operator_conditions":
                # This is a ClusterOperator condition metric
                condition = metric.get("condition", "")
                # Prometheus stores "Available == 0" meaning "not available".
                # We translate this into a more readable string for the response.
                if condition == "Available":
                    condition = "Not Available"

                operator_conditions.append(
                    OperatorCondition(
                        name=metric.get("name", ""),  # ClusterOperator name (e.g. "etcd")
                        condition=condition,           # "Not Available" or "Degraded"
                        reason=metric.get("reason"),  # Optional reason code from the operator
                    )
                )

        # Return all three categories of parsed data
        return console_url, alerts, operator_conditions

    def query_cluster_metrics(
        self, cluster_id: str
    ) -> Tuple[str, List[Alert], List[OperatorCondition]]:
        """Query Thanos for alerts and operator conditions for a cluster.

        Returns (console_url, alerts, operator_conditions).
        """
        # Build the PromQL query string for this specific cluster
        query = self._build_query(cluster_id)
        logger.info(query)  # Log the query for debugging purposes

        # Calculate the Unix timestamp for `lookback_minutes` ago.
        # Thanos evaluates the PromQL expression at this point in time.
        # This is useful to get a stable snapshot rather than the current instant.
        query_time = (
            datetime.now() - timedelta(minutes=self.lookback_minutes)
        ).timestamp()

        # Read the Kubernetes service account token from disk to authenticate the request
        token = self._get_bearer_token()

        # Make an HTTP GET request to the Thanos instant query API endpoint.
        # - params: URL query parameters appended to the URL (?query=...&time=...)
        # - headers: Authorization header with Bearer token for auth
        # - verify: path to CA cert bundle for TLS verification (in-cluster certificate)
        # - timeout: max seconds before giving up
        response = httpx.get(
            f"{self.thanos_url}/api/v1/query",
            params={"query": query, "time": query_time},
            headers={"Authorization": f"Bearer {token}"},
            verify=SA_CA_PATH,   # Verify the TLS cert using the Kubernetes CA bundle
            timeout=self.timeout,
        )
        # raise_for_status() raises an exception if the HTTP status code is 4xx or 5xx
        response.raise_for_status()

        # Parse the JSON response and return the structured results
        return self._parse_response(response.json())
