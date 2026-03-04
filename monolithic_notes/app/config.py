"""Application configuration."""
# This module defines the central configuration object for the entire application.
# All settings (database, file upload limits, Thanos, plugins) live here in one place.

# os: standard Python library for reading environment variables
import os

# dataclass: decorator that auto-generates __init__, __repr__ etc. from field annotations
# field: lets you define default values for mutable types like lists
from dataclasses import dataclass, field

# List: type hint for typed lists (e.g. List[str] means a list of strings)
from typing import List


# @dataclass turns AppConfig into a data-holding class — Python auto-generates
# __init__ and other boilerplate. Each annotated attribute becomes a constructor argument
# with the given default value.
@dataclass
class AppConfig:
    """Application configuration.

    Values are loaded from config.yml, with environment variables
    overriding the app section settings.
    """

    # --- PostgreSQL database connection settings ---
    # The hostname or IP of the PostgreSQL server (default: localhost for local dev)
    postgres_host: str = "localhost"
    # Port number PostgreSQL listens on (default: 5432, the standard PostgreSQL port)
    postgres_port: int = 5432
    # The name of the database inside PostgreSQL to connect to
    postgres_db: str = "insights"
    # Database username for authentication
    postgres_user: str = "insights"
    # Database password for authentication
    postgres_password: str = "insights"

    # --- File upload limits ---
    # Maximum upload file size in bytes (100MB = 100 * 1024 * 1024 = 104857600)
    max_file_size: int = 104857600
    # Temporary directory where uploaded archives are stored before processing
    temp_upload_dir: str = "/tmp/insights-uploads"
    # Timeout in seconds for extracting an archive (5 minutes default)
    extract_timeout: int = 300

    # --- Insights-core output format ---
    # Fully qualified Python class name of the formatter used to render insights-core results.
    # This class controls how rule output is structured (JSON, text, etc.)
    format: str = "insights.formats._json.JsonFormat"

    # --- Insights-core component filtering ---
    # Optional list of component name prefixes to run. If empty, all single-node components run.
    # This lets you restrict which rules/parsers are executed during archive processing.
    target_components: List[str] = field(default_factory=list)

    # --- Archive size protection ---
    # Maximum size (in bytes) of the unpacked archive contents.
    # -1 means no limit. Useful to prevent huge archives from exhausting disk space.
    unpacked_archive_size_limit: int = -1

    # --- Thanos / Prometheus metrics querying ---
    # URL of the Thanos rbac-query-proxy — this is an in-cluster endpoint in OpenShift
    # that gives this service access to Prometheus metrics for all managed clusters.
    thanos_url: str = "https://rbac-query-proxy.open-cluster-management-observability.svc.cluster.local:8443"
    # How many seconds to wait before giving up on a Thanos query
    thanos_query_timeout: int = 10
    # How far back in time (in minutes) to look when querying Thanos for alerts/conditions
    thanos_query_lookback_minutes: int = 60

    # --- Plugin/rule package configuration ---
    # List of Python package names to load as insights-core rule plugins
    # e.g., ["ccx_rules_ocp.external"] loads all CCX OCP rules
    plugin_packages: List[str] = field(default_factory=list)
    # List of dicts that configure individual plugin components (e.g., enable/disable configs)
    plugin_configs: List[dict] = field(default_factory=list)

    @property
    def database_url(self) -> str:
        """Construct PostgreSQL connection URL from components."""
        # Builds a SQLAlchemy-compatible connection string from the individual parts.
        # Format: postgresql://user:password@host:port/database
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


# Mapping from environment variable name → (config attribute name, Python type to cast to)
# When an env var is set, its value overrides the corresponding AppConfig field.
# This allows secrets (like passwords) to be injected via environment at runtime
# rather than baked into config.yml.
_ENV_OVERRIDES = {
    "POSTGRES_HOST": ("postgres_host", str),       # Override DB host via env
    "POSTGRES_PORT": ("postgres_port", int),       # Override DB port (cast to int)
    "POSTGRES_DB": ("postgres_db", str),           # Override DB name
    "POSTGRES_USER": ("postgres_user", str),       # Override DB username
    "POSTGRES_PASSWORD": ("postgres_password", str),  # Override DB password (secret)
    "MAX_FILE_SIZE": ("max_file_size", int),       # Override max upload size
    "TEMP_UPLOAD_DIR": ("temp_upload_dir", str),   # Override temp directory path
    "THANOS_URL": ("thanos_url", str),             # Override Thanos endpoint URL
    "THANOS_QUERY_TIMEOUT": ("thanos_query_timeout", int),  # Override query timeout
    "THANOS_QUERY_LOOKBACK_MINUTES": ("thanos_query_lookback_minutes", int),  # Override lookback window
}


def apply_env_overrides(config: AppConfig) -> None:
    """Override AppConfig fields with values from environment variables."""
    # Iterate over each env var name and the (field, type) it maps to
    for env_var, (attr, type_fn) in _ENV_OVERRIDES.items():
        # os.environ.get returns None if the variable is not set — we skip those
        val = os.environ.get(env_var)
        if val is not None:
            # Cast the string env var value to the correct Python type (int, str, etc.)
            # then store it on the config object using setattr (equivalent to config.attr = value)
            setattr(config, attr, type_fn(val))
