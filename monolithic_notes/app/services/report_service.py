"""Service for report and rule hit business logic."""
# This module is responsible for reading stored cluster reports from the database,
# enriching each rule hit with human-readable content (from markdown/YAML files),
# and assembling the final v2 API response structure.

# json: standard library for parsing and producing JSON strings
import json
# logging: standard library for emitting structured log messages
import logging
# Dict, List: type hints for return values and parameters
from typing import Dict, List

# Session: SQLAlchemy type for a database session (used to run queries)
from sqlalchemy.orm import Session

# ORM model classes that map to database tables
from app.models import Report, RuleHit

# Pydantic schemas that define the shape of the API response
from app.schemas import ReportV2, ReportMetaV2, RuleHitDetailedResponse

# ContentService: provides rule content (description, reason, resolution, etc.) by rule ID
from app.services.content_service import ContentService

# Utility functions for data transformation
from app.utils.content import normalize_rule_fqdn, format_datetime_rfc3339
# ResponseBuilder: helper that converts a RuleHit + content data into a RuleHitDetailedResponse
from app.utils.response_builder import ResponseBuilder

# Module-level logger
logger = logging.getLogger(__name__)


class ReportService:
    """Service providing cluster reports and rule hits data."""

    def __init__(self, content_service: ContentService):
        """
        Initialize the report service.

        :param content_service: Content service instance
        """
        # Store the content service so we can look up rule metadata later
        self.content_service = content_service

    def get_cluster_report_v2(self, db: Session, cluster_id: str) -> ReportV2:
        """
        Fetch v2 format report for a specific cluster.

        :param db: Database session
        :param cluster_id: Cluster UUID
        :return: ReportV2 object
        :raises ValueError: If cluster report not found
        """
        logger.info(f"Fetching v2 report for cluster {cluster_id}")

        # Query the `reports` table for the row matching this cluster UUID.
        # .filter() narrows the query; .first() returns the first result or None.
        report = (
            db.query(Report)
            .filter(Report.cluster == cluster_id)
            .first()
        )

        # If no report exists yet for this cluster, raise an error so the route handler
        # can return HTTP 404 to the caller.
        if not report:
            logger.warning(f"Report not found for cluster {cluster_id}")
            raise ValueError(f"Cluster report not found for cluster {cluster_id}")

        # The `report` column in the database stores a JSON string of the processed results.
        # We need to double-parse it: once to get the outer dict, then again to get the
        # insights-core results (which are stored as a nested JSON string inside `results`).
        try:
            # First parse: turn the DB column string into a Python dict
            report_json = json.loads(report.report) if report.report else {}
            # Second parse: the "results" key inside that dict is itself a JSON string
            insights_results = json.loads(report_json.get("results", "{}"))
            # "reports" is the list of individual rule results from insights-core
            insights_reports = insights_results.get("reports", [])
        except (json.JSONDecodeError, KeyError) as e:
            # If parsing fails, log and continue with an empty list (no extra details)
            logger.warning(f"Failed to parse insights-core results: {e}")
            insights_reports = []

        # Build a lookup dictionary keyed by "component|key" so we can quickly find
        # the insights-core result for any given rule hit below.
        # component = the rule's module path (e.g. "ccx_rules_ocp.external.rules.my_rule.report")
        # key = the error key (e.g. "ERROR_KEY_NAME")
        insights_map = {}
        for ir in insights_reports:
            component = ir.get("component", "")
            key = ir.get("key", "")
            # Combine into a single lookup key, separated by "|"
            insights_map[f"{component}|{key}"] = ir

        # Query all rule hits (from the `rule_hits` table) for this cluster.
        # Each RuleHit row stores the rule FQDN and error key that fired.
        rule_hits = (
            db.query(RuleHit)
            .filter(RuleHit.cluster_id == cluster_id)
            .all()
        )

        # Build the detailed rule hit objects, enriched with content data
        rule_hits_detailed = self._build_rule_hits_v2(rule_hits, insights_map)

        # Format timestamps as RFC 3339 strings (e.g. "2024-01-15T10:30:00Z")
        # format_datetime_rfc3339 handles None gracefully (returns None)
        last_checked_at = format_datetime_rfc3339(report.last_checked_at)
        gathered_at = format_datetime_rfc3339(report.gathered_at)

        # Assemble the report metadata section
        meta = ReportMetaV2(
            cluster_name=cluster_id,                    # Use the cluster UUID as the name
            managed=False,                              # Not an ACM-managed cluster in this context
            count=len(rule_hits_detailed),              # Number of rules that fired
            last_checked_at=last_checked_at,            # When the archive was last processed
            gathered_at=gathered_at,                    # When the data was collected on the cluster
        )

        # Assemble the full v2 report: metadata + list of detailed rule hits
        report_v2 = ReportV2(
            meta=meta,
            data=rule_hits_detailed,
        )

        logger.info(
            f"Successfully fetched v2 report for cluster {cluster_id} with {len(rule_hits_detailed)} rule hits"
        )

        return report_v2

    def _build_rule_hits_v2(
        self, rule_hits: List[RuleHit], insights_map: Dict
    ) -> List[RuleHitDetailedResponse]:
        """
        Build v2 detailed rule hit responses.

        :param rule_hits: List of RuleHit model instances
        :param insights_map: Map of insights-core reports by component|key
        :return: List of RuleHitDetailedResponse objects
        """
        rule_hits_detailed = []

        for hit in rule_hits:
            # The rule_fqdn stored in the DB may have a ".report" suffix (e.g. "my_rule.report").
            # The content service indexes rules without this suffix, so we strip it before lookup.
            rule_fqdn_for_content = normalize_rule_fqdn(hit.rule_fqdn)

            # Ask the content service for all human-readable content for this rule+error_key pair.
            # content_data is a dict with keys like: description, reason, resolution, more_info, tags, etc.
            content_data = self.content_service.get_content(rule_fqdn_for_content, hit.error_key)

            if content_data:
                # Look up the insights-core result details for this specific rule hit.
                # The key uses the original (unsuffixed) rule_fqdn from the DB.
                insights_key = f"{hit.rule_fqdn}|{hit.error_key}"
                insights_report = insights_map.get(insights_key, {})
                # "details" inside the insights-core result is a dict of rule-specific template data
                insights_details = insights_report.get("details", {})

                # Use ResponseBuilder to assemble the final RuleHitDetailedResponse object,
                # combining the database row, content data, and insights-core result details.
                rule_hit_detailed = ResponseBuilder.build_rule_hit_v2(
                    hit,                              # The RuleHit ORM model row
                    content_data,                     # Human-readable content from YAML/markdown
                    insights_details,                 # Extra template data from insights-core
                    content_data.get("publish_date"), # Publication date for the rule
                )

                rule_hits_detailed.append(rule_hit_detailed)
            else:
                # If content is missing for a rule hit, skip it and log a warning.
                # This can happen if the rules-content image doesn't include this rule's metadata.
                logger.warning(
                    f"Content not found for rule {hit.rule_fqdn}, error_key {hit.error_key}"
                )

        return rule_hits_detailed
