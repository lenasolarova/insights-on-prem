"""FastAPI application for Insights On Premise."""
# This is the entry point of the web application. It creates the FastAPI app,
# wires together all the services, and defines all HTTP endpoints (routes).

# logging: standard Python library for writing log messages (info, warning, error, etc.)
import logging
# os: standard library for interacting with the operating system (e.g. creating directories)
import os
# uuid: standard library for generating universally unique identifiers (UUIDs)
import uuid

# asynccontextmanager: decorator that allows an async function to act as a context manager
# (used here for the lifespan startup/shutdown hook)
from contextlib import asynccontextmanager

# FastAPI: the main web framework class used to build the API
# BackgroundTasks: FastAPI utility to schedule work to run after sending an HTTP response
# File, UploadFile: FastAPI types for handling multipart file uploads
# Request: the raw incoming HTTP request object
# Depends: declares dependencies that FastAPI auto-resolves (like injecting a DB session)
# HTTPException: exception class that produces an HTTP error response
# Header: declares an HTTP header parameter for a route function
from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile, Depends, HTTPException, Header

# JSONResponse: lets us manually build and return a JSON HTTP response
from fastapi.responses import JSONResponse

# Session: SQLAlchemy type representing a database session (one conversation with the DB)
from sqlalchemy.orm import Session

# load_config: reads config.yml and env vars, returns an AppConfig object
# load_insights_components: initializes insights-core plugins/rules from the config
from app.config_loader import load_config, load_insights_components

# init_db: creates the database engine and session factory, runs migrations
# get_db: a FastAPI dependency that yields a fresh DB session per request
from app.database import init_db, get_db

# Import the Pydantic schema classes that define the shape of request/response bodies.
# FastAPI uses these to automatically validate input and serialize output.
from app.schemas import (
    UploadResponse,                  # Response schema for successful archive uploads
    ErrorResponse,                   # Response schema for error cases
    ReportResponseV2,                # Response schema for the v2 cluster report endpoint
    UpgradeRisksPredictionRequest,   # Request body schema for upgrade prediction
    UpgradeRisksPredictionResponse,  # Response schema for upgrade prediction
)

# YAMLContentParser: reads rule metadata from YAML/markdown files on disk
from app.content_parser_yaml import YAMLContentParser

# Service classes — each handles one area of the application's business logic
from app.services.report_service import ReportService           # Fetches and formats cluster reports
from app.services.upload_service import UploadService           # Handles archive file upload and validation
from app.services.processor_service import ProcessorService     # Runs insights-core analysis on archives
from app.services.content_service import ContentService         # Serves rule metadata content
from app.services.thanos_service import ThanosService           # Queries Thanos/Prometheus metrics
from app.services.upgrade_prediction_service import UpgradePredictionService  # Predicts upgrade risks

# ValidationError: custom application-level exception for invalid uploads
from app.exceptions import ValidationError

# Get a logger for this module. Log messages will be prefixed with "app.main"
logger = logging.getLogger(__name__)


# @asynccontextmanager turns this async function into a context manager.
# FastAPI's `lifespan` parameter accepts a context manager that runs startup code
# before `yield` and shutdown code after `yield`. This replaces the older
# @app.on_event("startup") / @app.on_event("shutdown") pattern.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP: this code runs before the app begins accepting requests ---

    # Load application configuration from config.yml + environment variable overrides
    config = load_config()

    # Create the temporary upload directory if it doesn't exist yet.
    # exist_ok=True means no error is raised if the directory already exists.
    os.makedirs(config.temp_upload_dir, exist_ok=True)
    logger.info(f"Temp upload directory: {config.temp_upload_dir}")

    # Connect to the database and create the session factory.
    # init_db returns the engine (low-level DB connection) and session_factory (for creating sessions).
    engine, session_factory = init_db(config.database_url)
    # Store engine and session_factory on app.state so routes can access them
    app.state.engine = engine
    app.state.session_factory = session_factory
    logger.info("Database initialized successfully")

    # Load insights-core rule plugins (packages listed in config.yml under `plugins.packages`)
    # This registers all the rule classes with insights-core's dependency runner (dr)
    load_insights_components(config)

    # Instantiate all service objects and store them on app.state so route handlers can retrieve them.
    # This is a simple form of dependency injection — services are created once and reused.
    app.state.processor_service = ProcessorService(config)  # Insights-core runner
    app.state.upload_service = UploadService(app.state.processor_service, config, session_factory)  # Upload orchestrator
    app.state.content_service = ContentService(YAMLContentParser())  # Rule metadata loader
    app.state.report_service = ReportService(app.state.content_service)  # Report builder
    app.state.thanos_service = ThanosService(config)  # Prometheus/Thanos client
    app.state.upgrade_prediction_service = UpgradePredictionService()  # Risk predictor
    logger.info("All services initialized successfully")

    # yield pauses this function and hands control back to FastAPI.
    # The app now starts serving requests. When the app shuts down, code after
    # yield runs (none here, but this is where you'd close connections, etc.)
    yield


# Create the FastAPI application instance.
# title, description, and version appear in the auto-generated /docs Swagger UI.
# lifespan= registers the startup/shutdown hook defined above.
app = FastAPI(
    title="Insights On-Premise",
    description="Red Hat Insights archive processing for on-premise deployment",
    version="1.0.0",
    lifespan=lifespan
)


# Route: GET /
# Simple root endpoint. Useful for a quick sanity check that the service is alive.
@app.get("/")
async def root():
    """Root endpoint for health check."""
    # Returns a plain JSON object with service info
    return {
        "service": "insights-on-premise",
        "status": "running",
        "version": "1.0.0",
    }


# Route: GET /health
# Standard health check endpoint — monitoring systems poll this to know if the app is up.
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# Route: POST /api/ingress/v1/upload
# This is the main upload endpoint. The insights-operator sends compressed archives here.
# response_model=UploadResponse: FastAPI will validate and serialize the return value using this schema
# status_code=202: HTTP "Accepted" — we accept the file but processing happens in the background
# responses={...}: documents possible error responses in the Swagger UI
@app.post(
    "/api/ingress/v1/upload",
    response_model=UploadResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def upload_archive(
    request: Request,          # The raw HTTP request, used to access app.state services
    background_tasks: BackgroundTasks,  # FastAPI injects this so we can schedule background work
    file: UploadFile = File(...),  # The uploaded file from a multipart/form-data POST body
    # Optional custom request ID header from the insights-operator client.
    # alias= maps the Python parameter name to the actual HTTP header name.
    x_rh_insights_request_id: str = Header(None, alias="x-rh-insights-request-id"),
):
    """
    Upload and process Red Hat Insights archive.

    :param file: Uploaded archive file (tar, tar.gz, or tgz format)
    :param background_tasks: FastAPI background tasks
    :param x_rh_insights_request_id: Optional request ID header
    :return: UploadResponse with accepted status
    :raises HTTPException: On validation errors
    """
    # Retrieve the UploadService stored on app.state during startup
    upload_service: UploadService = request.app.state.upload_service

    # Use the provided request ID if present, otherwise generate a new UUID.
    # This ID ties together all log messages for a single upload request.
    request_id = x_rh_insights_request_id or str(uuid.uuid4())

    try:
        # Delegate all upload logic to the UploadService:
        # - validates the file type and size
        # - saves it to a temp file
        # - schedules background processing
        # - returns an UploadResponse immediately (202 Accepted)
        return await upload_service.process_upload(background_tasks, file, request_id)

    except ValidationError as e:
        # ValidationError means the client sent something invalid (wrong file type, too large, etc.)
        # Convert to an HTTP 400 Bad Request response
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    except Exception as e:
        # Catch-all for unexpected errors — log with full traceback and return HTTP 500
        logger.error(f"Request {request_id}: Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error during upload processing",
        )


# Route: GET /api/v2/cluster/{cluster_id}/reports
# Returns the latest processed report for a given cluster in v2 API format.
# {cluster_id} in the path is a URL path parameter that FastAPI extracts automatically.
# response_model=ReportResponseV2: the returned data is validated/serialized against this schema
@app.get(
    "/api/v2/cluster/{cluster_id}/reports",
    response_model=ReportResponseV2,
    status_code=200,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Cluster report not found"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def get_cluster_report_v2(
    request: Request,    # Raw HTTP request, used to access app.state
    cluster_id: str,     # Extracted from the URL path — the cluster's UUID
    # Depends(get_db) tells FastAPI to call get_db() and pass its result as `db`.
    # get_db is a generator that yields a DB session and closes it when the request is done.
    db: Session = Depends(get_db),
):
    """
    Retrieve the latest report for a specific cluster (v2 endpoint).

    This endpoint returns the latest report for the given cluster ID,
    following the v2 API format used by insights-results-smart-proxy.

    :param cluster_id: Cluster UUID
    :param db: Database session
    :return: ReportResponseV2 with detailed report data
    :raises HTTPException: On not found or processing errors
    """
    # Get the report service from app state
    report_service: ReportService = request.app.state.report_service

    try:
        # Delegate to the ReportService to fetch and build the v2 report structure
        report_v2 = report_service.get_cluster_report_v2(db, cluster_id)
        # Wrap the report in a ReportResponseV2 envelope with a status field
        return ReportResponseV2(
            report=report_v2,
            status="ok",
        )

    except ValueError as e:
        # ReportService raises ValueError when no report exists for this cluster
        raise HTTPException(
            status_code=404,
            detail=str(e),
        )

    except Exception as e:
        # Any other unexpected error
        logger.error(
            f"Error fetching v2 report for cluster {cluster_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error while fetching cluster report",
        )


# Route: POST /upgrade-risks-prediction
# Accepts a cluster ID and returns a prediction of whether it is safe to upgrade.
# This works by querying Thanos for live cluster health metrics and applying filter rules.
@app.post(
    "/upgrade-risks-prediction",
    response_model=UpgradeRisksPredictionResponse,
    status_code=200,
    responses={
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def upgrade_risks_prediction(
    request: Request,                     # Raw request, used to access app.state services
    body: UpgradeRisksPredictionRequest,  # JSON body parsed and validated using this Pydantic schema
):
    """
    Predict upgrade risks for a cluster based on current alerts and operator conditions.

    Queries Thanos for active alerts and failing operator conditions,
    then applies static filtering rules to identify actual upgrade risks.

    :param body: Request body containing cluster_id
    :return: UpgradeRisksPredictionResponse with recommendation and risks
    """
    # Retrieve services from app.state
    thanos_service: ThanosService = request.app.state.thanos_service
    prediction_service: UpgradePredictionService = request.app.state.upgrade_prediction_service

    try:
        # Step 1: Query Thanos (Prometheus) for:
        #   - console_url: URL for linking to the cluster's OpenShift console
        #   - alerts: active ALERTS metrics for this cluster
        #   - focs: failing cluster operator conditions (Degraded / Not Available)
        console_url, alerts, focs = thanos_service.query_cluster_metrics(body.cluster_id)

        # Step 2: Apply filtering logic to determine which alerts/conditions are real risks.
        # Returns an UpgradeRisksPredictionResponse with upgrade_recommended=True/False
        return prediction_service.predict(alerts, focs, console_url)

    except Exception as e:
        logger.error(
            f"Error predicting upgrade risks for cluster {body.cluster_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error while predicting upgrade risks",
        )


# Register a global exception handler for all HTTPException instances.
# FastAPI calls this function whenever any route raises an HTTPException.
# This allows us to format error responses consistently as ErrorResponse JSON objects.
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    """
    Custom handler for HTTP exceptions.

    :param request: HTTP request object
    :param exc: HTTPException instance
    :return: JSONResponse with error details
    """
    return JSONResponse(
        status_code=exc.status_code,
        # Build an ErrorResponse Pydantic model and serialize it to a dict for JSON output.
        # .dict() converts the Pydantic model to a plain Python dictionary.
        content=ErrorResponse(
            error=exc.detail,
            # Include the request ID from the HTTP header if present (helps trace requests in logs)
            request_id=request.headers.get("x-rh-insights-request-id"),
        ).dict(),
    )


# This block only runs when you execute this file directly (e.g. `python app/main.py`).
# In production the container uses `uvicorn app.main:app` directly instead.
if __name__ == "__main__":
    # uvicorn is a high-performance ASGI web server for running async FastAPI apps
    import uvicorn

    uvicorn.run(
        "app.main:app",   # Module path to the `app` FastAPI instance
        host="0.0.0.0",   # Bind to all network interfaces (not just localhost)
        port=8000,         # Listen on port 8000
        reload=True,       # Auto-restart when source files change (dev mode only)
    )
