"""Service for upload orchestration and validation."""
# This module handles everything related to receiving an uploaded archive file:
# validating its format, saving it to disk temporarily, and scheduling it for
# background processing. The goal is to respond to the HTTP request immediately
# (HTTP 202 Accepted) and do the heavy work asynchronously.

# logging: standard Python library for structured log output
import logging
# os: standard library for filesystem operations (removing files, checking existence)
import os
# tempfile: standard library for creating secure temporary files
import tempfile
# datetime: standard library type for timestamp values
from datetime import datetime
# Tuple: type annotation for functions that return two values
from typing import Tuple

# FastAPI types for handling file uploads and deferred (background) tasks
from fastapi import BackgroundTasks, UploadFile
# sessionmaker: SQLAlchemy factory that creates database Session objects on demand
from sqlalchemy.orm import sessionmaker

# Application configuration dataclass (holds max_file_size, temp_upload_dir, etc.)
from app.config import AppConfig
# Pydantic schema for the HTTP 202 response returned to the uploader
from app.schemas import UploadResponse
# ProcessorService: the service that actually runs insights-core analysis on the archive
from app.services.processor_service import ProcessorService
# Custom exception for user-caused validation errors (triggers HTTP 400 responses)
from app.exceptions import ValidationError

# Module-level logger
logger = logging.getLogger(__name__)


class UploadService:
    """Service for handling archive uploads and processing orchestration."""

    def __init__(
        self,
        processor_service: ProcessorService,  # Injected service for insights-core processing
        config: AppConfig,                    # App config (file size limits, temp dir)
        session_factory: sessionmaker,        # Factory for creating DB sessions in background tasks
    ):
        """
        Initialize the upload service.

        :param processor_service: Processor service instance
        :param config: Application configuration
        :param session_factory: SQLAlchemy session factory for background tasks
        """
        # Save dependencies for use in methods
        self.processor_service = processor_service
        self.config = config
        self.session_factory = session_factory

    def _get_archive_suffix(self, file: UploadFile) -> str:
        # Inspect the filename to determine which archive format was uploaded.
        # Returns the matching suffix string or an empty string if none matched.
        # The order matters — check ".tar.gz" before ".tar" to avoid a partial match.
        suffix = ""
        if file.filename.endswith('.tar.gz'):
            suffix = '.tar.gz'   # Gzip-compressed tar archive
        elif file.filename.endswith('.tgz'):
            suffix = '.tgz'      # Alternative extension for gzip-compressed tar
        elif file.filename.endswith('.tar'):
            suffix = '.tar'      # Uncompressed tar archive
        return suffix

    def _validate_file(self, file: UploadFile, request_id: str) -> None:
        """
        Validate uploaded file.

        :param file: Uploaded file
        :param request_id: Request ID for logging
        :raises ValidationError: If validation fails
        """
        # Ensure a filename was actually provided in the upload
        if not file.filename:
            logger.warning(f"Request {request_id}: No filename provided")
            raise ValidationError("No filename provided")

        # Ensure the file has an accepted archive extension.
        # If _get_archive_suffix returns "" it means the extension wasn't recognised.
        if self._get_archive_suffix(file) == "":
            logger.warning(f"Request {request_id}: Invalid file format: {file.filename}")
            raise ValidationError("File must be a .tar, .tar.gz, or .tgz archive")

    async def _save_to_temp(
        self, file: UploadFile, request_id: str
    ) -> Tuple[str, int]:
        """
        Save uploaded file to temporary location.

        :param file: Uploaded file
        :param request_id: Request ID for logging
        :return: Tuple of (temp_file_path, total_size)
        :raises ValidationError: If file size exceeds limit
        """
        # Create a temporary file in the configured temp directory.
        # delete=False: the file persists after this block closes it (we clean it up later).
        # suffix=: preserves the original archive extension so insights-core can detect the format.
        # dir=: places the file in the configured temp directory (not the OS default /tmp).
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=self._get_archive_suffix(file),
            dir=self.config.temp_upload_dir,
        ) as temp_file:
            temp_file_path = temp_file.name  # Full filesystem path to the created temp file

            # Read the uploaded data in chunks to avoid loading the entire file into RAM.
            # This is important for large archives (up to 100MB by default).
            chunk_size = 1024 * 1024  # 1MB per chunk
            total_size = 0

            while True:
                # await: because UploadFile.read() is async (non-blocking I/O)
                chunk = await file.read(chunk_size)
                if not chunk:
                    # Empty chunk means we've reached the end of the file
                    break

                # Accumulate the running total of bytes received so far
                total_size += len(chunk)

                # Enforce the maximum allowed file size.
                # We check incrementally (chunk by chunk) rather than all at once
                # to avoid storing an oversized file to disk before rejecting it.
                if total_size > self.config.max_file_size:
                    # Remove the partially-written temp file to avoid disk space waste
                    try:
                        os.remove(temp_file_path)
                    except:
                        pass  # Silently ignore cleanup errors — not critical

                    logger.warning(
                        f"Request {request_id}: File too large ({total_size} bytes)"
                    )
                    raise ValidationError(
                        f"File size exceeds maximum allowed size of {self.config.max_file_size} bytes"
                    )

                # Write this chunk to the temp file on disk
                temp_file.write(chunk)

        logger.info(
            f"Request {request_id}: Saved uploaded file ({total_size} bytes) to {temp_file_path}"
        )

        # Return the path to the temp file and the total bytes received
        return temp_file_path, total_size

    def _process_in_background(self, temp_file_path: str, request_id: str) -> None:
        """
        Process archive in a background task.

        :param temp_file_path: Path to temporary archive file
        :param request_id: Request ID for logging
        """
        # This method runs AFTER the HTTP response has already been sent to the client.
        # It performs the full insights-core analysis and saves results to the database.
        try:
            # Create a new database session for this background task.
            # Background tasks run outside the HTTP request/response cycle, so they need
            # their own session rather than the one created by the get_db dependency.
            db = self.session_factory()
            try:
                # Run the full archive processing pipeline:
                # 1. Extract the archive
                # 2. Run insights-core rules against the extracted data
                # 3. Save rule hits and the report to the database
                cluster_id, rules_count = self.processor_service.process_archive(
                    db, temp_file_path
                )
                logger.info(
                    f"Request {request_id}: Successfully processed cluster {cluster_id} "
                    f"with {rules_count} rules"
                )
            except Exception as e:
                # Log the error but don't crash the background task thread
                logger.error(f"Request {request_id}: Background processing failed: {e}", exc_info=True)
            finally:
                # Always close the DB session when done (even if an exception occurred)
                db.close()
        finally:
            # Always clean up the temp file after processing, success or failure.
            # This prevents the temp directory from filling up with stale archives.
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.debug(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary file: {e}")

    async def process_upload(
        self, background_tasks: BackgroundTasks, file: UploadFile, request_id: str
    ) -> UploadResponse:
        """
        Validate and save upload, then schedule processing as a background task.

        :param background_tasks: FastAPI BackgroundTasks
        :param file: Uploaded file
        :param request_id: Request ID
        :return: UploadResponse with accepted status
        :raises ValidationError: On validation errors
        """
        logger.info(f"Upload request {request_id}")

        # Step 1: Validate the file extension and that a filename exists.
        # Raises ValidationError (HTTP 400) if the file is invalid.
        self._validate_file(file, request_id)

        # Step 2: Asynchronously stream the file to a temp location on disk,
        # enforcing the max file size limit along the way.
        temp_file_path, total_size = await self._save_to_temp(file, request_id)

        # Step 3: Schedule the heavy processing work (insights-core analysis + DB write)
        # to run AFTER this function returns and the HTTP response is sent.
        # background_tasks.add_task() queues a callable with its arguments.
        background_tasks.add_task(self._process_in_background, temp_file_path, request_id)

        # Step 4: Immediately return HTTP 202 Accepted — the client knows the upload
        # was received and processing will happen asynchronously.
        return UploadResponse(
            request_id=request_id,       # Echo back the request ID for correlation
            status="accepted",           # Indicates the file was received, not yet processed
            uploaded_at=datetime.utcnow(),  # Current UTC timestamp
        )
