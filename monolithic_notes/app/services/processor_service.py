"""Insights-core archive processing service."""
# This module is the heart of the application. It takes a compressed archive file
# (tar.gz) produced by the Red Hat Insights data collector running on an OpenShift cluster,
# extracts it, runs insights-core rule analysis over the extracted data, and saves the
# findings to the database. insights-core is a Red Hat open-source framework that
# provides parsers, combiners, and rules for analyzing system state.

# json: standard library for encoding/decoding JSON strings
import json
# logging: standard library for structured log messages
import logging
# datetime: standard library type for timestamps
from datetime import datetime
# StringIO: in-memory file-like object used to capture text output from insights-core
from io import StringIO
# Path: object-oriented filesystem path library (for recursive file size calculation)
from pathlib import Path
# Dict, List, Tuple: type annotations
from typing import Dict, List, Tuple

# Session: SQLAlchemy type for a database session
from sqlalchemy.orm import Session

# --- insights-core library imports ---
# dr: the "dependency runner" — insights-core's central module registry and execution engine.
#     It knows about all registered components (parsers, combiners, rules) and can
#     run them in topological order (respecting dependencies).
from insights import dr

# extract: context manager that decompresses a .tar.gz archive into a temporary directory
from insights.core.archives import extract

# initialize_broker: sets up the "broker" — an insights-core object that stores parsed
#     component outputs and makes them available to downstream components (rules, combiners)
from insights.core.hydration import initialize_broker

# HumanReadableFormat: the default insights-core output formatter (plain text).
#     Used as a fallback if the configured formatter class cannot be found.
from insights.formats.text import HumanReadableFormat

# Application-level imports
from app.config import AppConfig
# ORM model classes for saving data to the database
from app.models import Report, RuleHit
# Custom exception for processing failures (triggers HTTP 500 upstream)
from app.exceptions import ProcessingError

# Module-level logger
logger = logging.getLogger(__name__)


class ProcessorService:
    """
    Service for processing Red Hat Insights archives.
    Refactored from ArchiveProcessor to use dependency injection.
    """

    def __init__(self, config: AppConfig):
        """
        Initialize the processor service.

        :param config: Application configuration
        """
        # Store config for later use (extract timeout, temp dir, size limits)
        self.config = config

        # Resolve the output formatter class by its fully qualified Python name.
        # dr.get_component() looks up a class by its module.ClassName string.
        # If the class is not found (e.g. the package is not installed), fall back to
        # HumanReadableFormat which always exists in core insights-core.
        self.Formatter = dr.get_component(config.format) or HumanReadableFormat

        # Build the set of insights-core components (parsers, combiners, rules) to run.
        if config.target_components:
            # If specific component name prefixes are configured, only include those
            # components and their transitive dependencies.
            self.components_dict = self._get_component_graphs(config.target_components)
        else:
            # If no target components are specified, run ALL "single" components.
            # dr.COMPONENTS[dr.GROUPS.single] is the set of all components that operate
            # on a single system (as opposed to multi-system combiners).
            # dr.determine_components() resolves the full dependency closure for them.
            self.components_dict = dr.determine_components(
                dr.COMPONENTS[dr.GROUPS.single]
            )

        # Flatten the dependency graph into an execution order list using topological sort.
        # sort=False means we don't sort alphabetically — just use the dependency order.
        self.target_components = dr.toposort_flatten(self.components_dict, sort=False)

        # Save extraction settings from config
        self.extract_timeout = config.extract_timeout           # Max seconds for extraction step
        self.extract_tmp_dir = config.temp_upload_dir           # Directory for extracted archive contents
        self.unpacked_archive_size_limit = config.unpacked_archive_size_limit  # Max unpacked size in bytes

        logger.debug(
            f"Processor initialized with {len(self.target_components)} components"
        )

    def _get_component_graphs(self, target_components: List[str]) -> Dict:
        """
        Get dependency graphs for target components.

        :param target_components: List of component name prefixes
        :return: Dictionary of component dependency graphs
        """
        graph = {}
        # Convert the list to a tuple so we can use str.startswith(tuple) — Python allows
        # passing a tuple of prefixes to startswith(), which checks any of them.
        tc = tuple(target_components or [])

        if tc:
            # Iterate over every registered component in the insights-core dependency registry
            for c in dr.DELEGATES:
                # dr.get_name(c) returns the fully qualified name of component c.
                # Only include components whose name starts with one of the configured prefixes.
                if dr.get_name(c).startswith(tc):
                    # dr.get_dependency_graph(c) returns a dict of c + all its dependencies.
                    # We merge these dicts together to build the complete graph.
                    graph.update(dr.get_dependency_graph(c))

        return graph

    def _validate_size(self, extraction_path: str) -> bool:
        """
        Validate unpacked archive size.

        :param extraction_path: Path to extracted archive
        :return: True if size is acceptable, False otherwise
        """
        # -1 means unlimited — skip the check entirely
        if self.unpacked_archive_size_limit < 0:
            logger.debug("No size limitation for unpacked archive")
            return True

        # Recursively walk every file under the extraction directory and sum their sizes.
        # Path.rglob("*") yields all files and directories recursively.
        # p.stat().st_size returns the file size in bytes (st = stat, size in bytes).
        total_size = sum(p.stat().st_size for p in Path(extraction_path).rglob("*"))

        if total_size >= self.unpacked_archive_size_limit:
            logger.warning(
                f"Unpacked archive exceeds limit: {total_size} >= {self.unpacked_archive_size_limit}"
            )
            return False  # Archive is too big

        return True  # Size is acceptable

    def get_cluster_id(self, extraction_path: str) -> str:
        """
        Extract cluster ID from archive.

        :param extraction_path: Path to extracted archive directory
        :return: Cluster identifier
        :raises ProcessingError: If cluster ID cannot be determined
        """
        # os.path is imported locally here (it's usually imported at the top of a file,
        # but this function imports it inline — this is valid Python though unconventional)
        import os

        # The insights-operator archive contains a file at "config/id" that holds the
        # OpenShift cluster's UUID. This is the standard location for the cluster identifier.
        id_file_path = os.path.join(extraction_path, "config", "id")
        if os.path.exists(id_file_path):
            try:
                with open(id_file_path, "r") as f:
                    # Read the cluster ID and strip any surrounding whitespace/newlines
                    cluster_id = f.read().strip()
                    if cluster_id:
                        logger.info(f"Found cluster_id in config/id: {cluster_id}")
                        return cluster_id
            except Exception as e:
                logger.error(f"Failed to read config/id: {e}")
                raise ProcessingError(f"Failed to read config/id: {str(e)}")

        # If the config/id file doesn't exist, we cannot identify the cluster — fail loudly
        raise ProcessingError("Could not find cluster ID. Missing config/id file in archive.")

    def process_with_insights_core(self, archive_path: str) -> Tuple[str, str]:
        """
        Process archive with insights-core.

        :param archive_path: Path to archive file
        :return: Tuple of (cluster_id, results_json)
        :raises ProcessingError: If processing fails
        """
        try:
            logger.info(f"Processing archive: {archive_path}")

            # Use insights.core.archives.extract() as a context manager.
            # It decompresses the archive into a temporary directory and cleans
            # up that directory automatically when the `with` block exits.
            # - timeout: maximum seconds to spend extracting (prevents hung processes)
            # - extract_dir: parent directory for the temporary extraction subdirectory
            with extract(
                archive_path,
                timeout=self.extract_timeout,
                extract_dir=self.extract_tmp_dir,
            ) as extraction:
                # extraction.tmp_dir is the path to the extracted archive contents

                # Check that the unpacked size is within configured limits
                if not self._validate_size(extraction.tmp_dir):
                    raise ProcessingError(
                        f"Archive exceeds size limit: {self.unpacked_archive_size_limit}"
                    )

                # Read the cluster UUID from inside the extracted archive
                cluster_id = self.get_cluster_id(extraction.tmp_dir)
                logger.info(f"Processing cluster: {cluster_id}")

                # Set up the insights-core broker.
                # initialize_broker() scans the extracted directory, finds system files,
                # and creates a broker object that components can query for their inputs.
                # ctx = the "context" that wraps the extraction path; broker = the component store
                ctx, broker = initialize_broker(extraction.tmp_dir)

                # Run all the configured insights-core components and capture their output.
                # StringIO() creates an in-memory text buffer (acts like an open file).
                output = StringIO()
                # The Formatter is used as a context manager: it writes formatted output
                # to `stream` as components are run, then finalizes on __exit__.
                with self.Formatter(broker, stream=output):
                    # dr.run_components() executes each component in topological order,
                    # feeding outputs from earlier components into later ones via the broker.
                    dr.run_components(
                        self.target_components,   # Ordered list of components to run
                        self.components_dict,     # Dependency graph
                        broker=broker,            # Shared data store
                    )

                # Rewind the in-memory buffer to the start and read all the output text
                output.seek(0)
                result = output.read()

                logger.info(f"Processing completed for cluster {cluster_id}")
                logger.debug(f"Result length: {len(result)} chars")

                # Return the cluster ID and the JSON-formatted insights-core output
                return cluster_id, result

        except Exception as e:
            logger.error(f"insights-core processing failed: {e}", exc_info=True)
            # Wrap the error in our custom exception type for uniform error handling upstream
            raise ProcessingError(f"Analysis failed: {str(e)}")

    def extract_rule_hits(self, results_json: str) -> List[Dict]:
        """
        Extract rule hits from insights-core results.

        :param results_json: JSON string from insights-core
        :return: List of rule hit dictionaries
        """
        rule_hits = []

        try:
            # Guard against empty or null output (valid in case no rules fired)
            if not results_json or results_json == "{}":
                logger.info("No results to parse")
                return rule_hits

            # Parse the JSON string produced by insights-core into a Python dict
            results = json.loads(results_json)

            # "reports" is the list of component outputs — each entry represents one
            # rule/combiner that produced output
            reports = results.get("reports", [])

            for report in reports:
                # We only care about entries with type="rule" — these are actual rule hits.
                # Other types (e.g. "skip") are informational and not treated as findings.
                if report.get("type") == "rule":
                    # "component" is the fully qualified rule class name (the FQDN)
                    component = report.get("component", "")
                    rule_fqdn = component
                    # "key" is the error key — the specific condition the rule identified
                    error_key = report.get("key", "UNKNOWN_ERROR")
                    # "details" is a rule-specific dict with extra template data
                    details = report.get("details", {})

                    if rule_fqdn:
                        # Collect this rule hit as a plain dict
                        rule_hits.append({
                            "rule_fqdn": rule_fqdn,
                            "error_key": error_key,
                            "details": details,
                        })

            logger.info(f"Extracted {len(rule_hits)} rule hits")

        except json.JSONDecodeError as e:
            # The output wasn't valid JSON — log but don't crash
            logger.error(f"Failed to parse results JSON: {e}")
        except Exception as e:
            # Catch-all for unexpected errors during parsing
            logger.error(f"Error extracting rule hits: {e}", exc_info=True)

        return rule_hits

    def save_results(self, db: Session, cluster_id: str, results_json: str) -> int:
        """
        Save processing results to database.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :param results_json: JSON results from insights-core
        :return: Number of rule hits saved
        """
        # Parse the raw JSON output into individual rule hit dicts
        rule_hits = self.extract_rule_hits(results_json)

        try:
            # Build the data dict to store in the Report table.
            # "results" stores the entire insights-core JSON output for later re-parsing.
            report_data = {
                "cluster_id": cluster_id,
                "rule_count": len(rule_hits),
                "processed_at": datetime.utcnow().isoformat(),  # When this processing run occurred
                "results": results_json,                         # Raw insights-core JSON output
            }

            # Upsert (insert or update) the Report row for this cluster.
            # If a report already exists for this cluster_id, it gets overwritten.
            Report.upsert(
                db,
                cluster=cluster_id,
                report=json.dumps(report_data),  # Serialize the dict back to JSON for storage
                gathered_at=datetime.utcnow(),
            )

            # Upsert each rule hit into the rule_hits table.
            # "upsert" preserves the original `impacted_since` timestamp for existing hits
            # (so we know how long a problem has been present).
            new_keys = set()  # Track which (rule_fqdn, error_key) pairs are in this run
            for hit in rule_hits:
                RuleHit.upsert(
                    db,
                    cluster_id=cluster_id,
                    rule_fqdn=hit["rule_fqdn"],
                    error_key=hit["error_key"],
                )
                # Add this pair to the set of currently-firing rules
                new_keys.add((hit["rule_fqdn"], hit["error_key"]))

            # Remove stale rule hits — rules that were firing in a previous archive
            # but are NOT present in this new archive (the problem has been resolved).
            existing_hits = db.query(RuleHit).filter_by(cluster_id=cluster_id).all()
            for existing in existing_hits:
                if (existing.rule_fqdn, existing.error_key) not in new_keys:
                    # This rule is no longer firing — delete its database row
                    db.delete(existing)

            # Commit all the inserts, updates, and deletes as a single atomic transaction
            db.commit()

            logger.info(f"Saved {len(rule_hits)} rule hits for cluster {cluster_id}")
            return len(rule_hits)

        except Exception as e:
            # Roll back the transaction to undo any partial writes if something went wrong
            db.rollback()
            logger.error(f"Failed to save results for cluster {cluster_id}: {e}", exc_info=True)
            raise ProcessingError(f"Database save failed: {str(e)}")

    def process_archive(self, db: Session, archive_path: str) -> Tuple[str, int]:
        """
        Main processing function - extract, analyze, and save archive.

        :param db: Database session
        :param archive_path: Path to uploaded archive file
        :return: Tuple of (cluster_id, number of rules found)
        :raises ProcessingError: If processing fails at any stage
        """
        logger.info(f"Starting archive processing: {archive_path}")

        # Step 1: Run insights-core analysis
        # Extracts the archive, identifies the cluster, runs all registered rules,
        # and returns the cluster UUID plus the formatted results JSON string.
        cluster_id, results_json = self.process_with_insights_core(archive_path)

        # Step 2: Persist the results to the database
        # Saves the Report row and all RuleHit rows, removing stale hits.
        rules_count = self.save_results(db, cluster_id, results_json)

        logger.info(f"Completed processing for cluster {cluster_id}")
        # Return both values so the caller can log them
        return cluster_id, rules_count
