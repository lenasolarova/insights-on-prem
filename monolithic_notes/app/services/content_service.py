"""
In-process content service that mimics insights-content-service behavior.

This module provides content service functionality by reading markdown/YAML files
directly from the rules-content directory, matching the behavior of the Go-based
insights-content-service but implemented in Python.
"""
# BACKGROUND:
# In the standard Red Hat Insights cloud architecture, a separate Go microservice called
# "insights-content-service" serves rule metadata (descriptions, reasons, resolutions) over HTTP.
# In this on-premise monolithic setup, we replicate that behavior inside the Python app itself:
# we read the same markdown/YAML content files from disk at startup, index them in memory,
# and serve them directly — no separate HTTP service needed.

# logging: standard Python library for log messages
import logging
# Dict, Optional: type annotations for return values
from typing import Dict, Optional

# YAMLContentParser: the class that actually reads the markdown/YAML files from disk
# and parses them into Python dicts
from app.content_parser_yaml import YAMLContentParser

# Module-level logger
logger = logging.getLogger(__name__)


class ContentService:
    """
    In-process content service.

    Mimics insights-content-service by serving rule metadata from
    markdown/YAML files. Loads all content at initialization and keeps
    it in memory, just like the Go-based content-service does.
    """

    def __init__(self, parser: YAMLContentParser):
        """
        Initialize content service.

        :param parser: Content parser instance
        """
        # Store the parser so we can call it during startup
        self.parser = parser

        # Internal storage for the content index.
        # Keys are (rule_fqdn, error_key) tuples; values are dicts of rule metadata.
        # A dict keyed by tuple is an efficient way to do exact-match lookups later.
        self._content_index: Dict[tuple, Dict] = {}

        # A flat list of all parsed rules (used if we need to return all rules at once)
        self._all_content: list = []

        # Load all content from disk immediately at construction time.
        # This means the first call to get_content() is fast (no lazy loading needed).
        self._load_content()

    def _load_content(self):
        """Load all content from files into memory."""
        logger.info("Loading rule content from files...")

        # Ask the parser to scan the content directory and return all parsed rules.
        # Each rule is a dict like:
        # {
        #   "rule_fqdn": "ccx_rules_ocp.external.rules.my_rule",
        #   "error_key": "MY_ERROR_KEY",
        #   "description": "...",
        #   "reason": "...",
        #   "resolution": "...",
        #   "more_info": "...",
        #   "tags": [...],
        #   "total_risk": 2,
        #   ...
        # }
        all_rules = self.parser.parse_all_rules()

        # Build a lookup index from (rule_fqdn, error_key) → rule dict.
        # This allows O(1) lookups when building report responses.
        for rule in all_rules:
            key = (rule["rule_fqdn"], rule["error_key"])
            self._content_index[key] = rule

        # Also keep the full list for potential future use
        self._all_content = all_rules

        logger.info(f"Loaded {len(self._content_index)} rules into memory")

    def get_content(self, rule_fqdn: str, error_key: str) -> Optional[Dict]:
        """
        Get content for a specific rule and error key.

        :param rule_fqdn: Fully qualified rule name
        :param error_key: Error key
        :return: Rule content dictionary or None if not found
        """
        # Build the lookup tuple from the two-part key
        key = (rule_fqdn, error_key)
        # Look up in the index dict — returns None if not found (dict.get default)
        content = self._content_index.get(key)

        if not content:
            # This can happen if a rule fired but its content file wasn't included
            # in the rules-content image. Log a warning so operators can investigate.
            logger.warning(f"Content not found for {rule_fqdn}:{error_key}")

        return content
