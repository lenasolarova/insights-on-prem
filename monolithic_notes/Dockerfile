# Dockerfile — Multi-stage build for the Insights On-Premise monolithic application
# ====================================================================================
# This Dockerfile uses a two-stage build:
#   Stage 1 ("rules"): pulls a pre-built image that contains the rules content files
#   Stage 2 (final):   builds the actual runnable Python application image
#
# Multi-stage builds let us copy specific files from another image without including
# that image's entire filesystem in the final output, keeping the image lean.

# ---- Stage 1: rules ----------------------------------------------------------------
# NOTE: This stage's image is NOT used at runtime. Its only purpose is to act as a
# source from which we can `COPY --from=rules` specific files into the final image.
# The rules-containers image contains pre-packaged rule content (YAML/markdown files
# and Python packages) built by the Red Hat CCX team.
FROM quay.io/redhat-services-prod/obsint-processing-tenant/rules-containers/rules-containers-private:latest AS rules

# ---- Stage 2: final runtime image --------------------------------------------------
# Start from the Red Hat Universal Base Image 9 (minimal variant).
# UBI9-minimal is a small, Red Hat supported base image suitable for production containers.
# It has microdnf (a lightweight RPM package manager) but fewer pre-installed utilities
# than the full UBI9 image, keeping the attack surface small.
FROM registry.access.redhat.com/ubi9-minimal:latest

# Set environment variables that persist in the final image and affect runtime behavior.
# ENV key=value \: the backslash continues the ENV instruction across multiple lines.
ENV HOME=/app \
    # VENV: the path where the Python virtual environment will be created.
    # Isolating Python packages in a venv prevents conflicts with system Python.
    VENV=/app-venv \
    # REQUESTS_CA_BUNDLE: tells the `requests` HTTP library (and anything using it)
    # where to find the CA certificates bundle for TLS verification.
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    # PYTHONUNBUFFERED=1: disables Python's output buffering so that log messages
    # appear in the container logs immediately (not held in a buffer that might flush late).
    PYTHONUNBUFFERED=1 \
    # Prepend the venv's bin directory to PATH so that `python` and `pip` resolve to the
    # venv versions rather than the system Python.
    PATH="/app-venv/bin:$PATH"

# Set the working directory inside the container. All subsequent COPY, RUN, CMD
# instructions use /app as their base. Also becomes the initial directory when you exec in.
WORKDIR $HOME

# Copy the Red Hat internal CA certificate from the rules image into this image's
# CA trust store. This is required for making TLS connections to internal Red Hat services.
# --from=rules: copy from the "rules" build stage (Stage 1), not from the build context.
COPY --from=rules /etc/pki/ca-trust/source/anchors/Current-IT-Root-CAs.pem /etc/pki/ca-trust/source/anchors/

# Install system-level dependencies and set up the Python virtual environment.
# All steps are combined in a single RUN instruction to minimize the number of image layers
# (each RUN creates a new layer; combining them keeps the image smaller).
RUN microdnf install --nodocs -y sqlite python3.12 python3.12-devel gcc postgresql-devel file tar unzip && \
    # Create a Python 3.12 virtual environment at $VENV (/app-venv).
    # The venv isolates our Python packages from the system Python installation.
    python3.12 -m venv $VENV && \
    # Run update-ca-trust to incorporate the CA cert we copied above into the system trust store.
    update-ca-trust && \
    # Upgrade pip (the Python package installer) and install setuptools + wheel.
    # --no-cache-dir: don't cache downloaded packages (saves image size).
    # -U: upgrade to the latest available version.
    pip install --no-cache-dir -U pip setuptools wheel && \
    # Remove the microdnf package manager cache to reduce the final image size.
    microdnf clean all && \
    # Create the temp uploads directory and make it world-writable.
    # 777 permissions are needed because the container runs as a non-root user (UID 1001)
    # who wouldn't otherwise have write access to directories they didn't create.
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

# Copy the Python requirements file FIRST, before any application code.
# Docker layer caching: if requirements.txt hasn't changed, Docker reuses the cached
# pip install layer below — skipping a potentially slow dependency installation step.
COPY requirements.txt .

# Install Python package dependencies from requirements.txt.
# --mount=type=cache,target=/root/.cache/pip: mount a BuildKit cache for pip's download cache.
# This cache persists across builds (but not in the final image), speeding up rebuilds
# when only the app code changes (not the dependencies).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy the application source code AFTER pip install.
# Because Python dependencies rarely change compared to application code, placing the
# COPY app / COPY config here means that changes to app code don't invalidate the
# expensive pip install layer above — we only reinstall packages when requirements.txt changes.
COPY app ./app             # The main Python application package
COPY migrations ./migrations  # Database migration scripts (Alembic)
COPY config.yml ./            # The main application configuration file

# Copy the pre-built rule content files from the "rules" build stage.
# /app/content/content in the rules image → /app/content in this image.
# These are the YAML and markdown files that ContentService reads for rule metadata.
COPY --from=rules /app/content/content /app/content

# Declare that the container listens on port 8000 at runtime.
# This is documentation only — it doesn't actually publish the port.
# The Kubernetes Service or docker run -p flag controls actual port mapping.
EXPOSE 8000

# Switch to a non-root user for security.
# Running as UID 1001 (non-root) follows OpenShift security best practices.
# OpenShift by default does not allow containers to run as root.
USER 1001

# The default command to run when the container starts.
# uvicorn is an ASGI web server that runs our FastAPI application.
# app.main:app means: import the `app` object from the `app.main` Python module.
# --host 0.0.0.0: listen on all network interfaces (not just loopback / localhost).
# --port 8000: listen on TCP port 8000.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
