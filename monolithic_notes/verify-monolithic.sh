#!/bin/bash
# verify-monolithic.sh — End-to-end verification of the monolithic insights pipeline
# ====================================================================================
# This script performs a series of checks to confirm that all parts of the
# on-premise insights pipeline are working correctly. It can be run at any time
# after deployment to check the health of the system.
#
# The pipeline this script verifies is:
#   insights-operator (OpenShift cluster)
#     → uploads archives to → insights-on-prem app (our service)
#       → stores results → ACM insights-client reads reports from our service
#         → upgrade-risks-prediction → Thanos (Prometheus metrics)
#
# Usage:
#   ./verify-monolithic.sh           # Brief output (pass/fail only)
#   ./verify-monolithic.sh -v        # Verbose output (includes matching log lines)
#   ./verify-monolithic.sh --verbose # Same as -v

# Default: verbose mode is off — only show pass/fail symbols
VERBOSE=false
# Check if the first argument ($1) is "-v" or "--verbose"; if so, enable verbose mode
if [[ "$1" == "-v" || "$1" == "--verbose" ]]; then
    VERBOSE=true
fi

# These variables define the target namespace and app name used for all `oc` commands below.
# Using variables means if the name changes, you only need to update it in one place.
NAMESPACE="insights-on-prem-poc"   # The Kubernetes namespace our app runs in
APP="insights-on-prem"             # The app label value used on the pod and deployment

echo "=== Monolithic Pipeline Verification ==="
echo

# --- Check 1: Is the application pod running? ---
echo -n "App running: "
# `oc get pod -l app=$APP -n $NAMESPACE` — list pods with the label app=insights-on-prem
# `-o jsonpath='{.items[0].status.phase}'` — extract just the phase of the first matching pod
# (Pod phase values: Pending, Running, Succeeded, Failed, Unknown)
# `2>/dev/null` — discard any error output (e.g. if no pods found)
PHASE=$(oc get pod -l app=$APP -n $NAMESPACE -o jsonpath='{.items[0].status.phase}' 2>/dev/null)
if [ "$PHASE" == "Running" ]; then
    echo "✓"   # Pod is in Running phase — healthy
else
    echo "❌ ($PHASE)"  # Pod is not Running — show current phase for debugging
fi

# --- Check 2: Did insights-operator successfully upload an archive? ---
echo -n "Insights upload: "
# Look at the last 10 minutes of insights-operator logs for a successful upload message.
# `|| true` prevents the script from exiting if grep finds no matches (would return exit code 1)
UPLOAD_LOG=$(oc logs -n openshift-insights deployment/insights-operator --since=10m 2>/dev/null | grep "Uploaded report successfully" || true)
if [ -n "$UPLOAD_LOG" ]; then
    echo "✓"   # Found the success message in logs
    # If verbose mode is on, print the matching log line(s) for more detail
    $VERBOSE && echo "  $UPLOAD_LOG"
else
    # Not found — the upload either hasn't happened yet or failed.
    # The hint suggests restarting the operator pod to trigger an immediate upload.
    echo "⚠️  (wait ~2min after: oc delete pod -n openshift-insights -l app=insights-operator)"
fi

# --- Check 3: Did our app receive the upload? ---
echo -n "App received upload: "
# Search our app's recent logs for a POST to the upload endpoint that returned 200 or 202.
# HTTP 202 = Accepted (expected response for uploads)
# HTTP 200 = OK (also acceptable)
# `tail -1` — show only the last (most recent) matching line
RECV_LOG=$(oc logs -n $NAMESPACE deployment/$APP --since=10m 2>/dev/null | grep "POST /api/ingress/v1/upload" | grep "200\|202" | tail -1 || true)
if [ -n "$RECV_LOG" ]; then
    echo "✓"
    $VERBOSE && echo "  $RECV_LOG"
else
    echo "❌"   # No recent upload received — pipeline may be broken before our service
fi

# --- Check 4: Was the archive processed and stored? ---
echo -n "Archive processed: "
# Look for the log message emitted by ProcessorService after successful processing
PROC_LOG=$(oc logs -n $NAMESPACE deployment/$APP --since=10m 2>/dev/null | grep "Successfully processed" | tail -1 || true)
if [ -n "$PROC_LOG" ]; then
    echo "✓"
    $VERBOSE && echo "  $PROC_LOG"
else
    echo "❌"   # Processing may have failed — check the app logs for errors
fi

# --- Check 5: Is ACM's insights-client querying our service? ---
# First check that the insights-client deployment exists (it's part of ACM and may not be installed)
if oc get deployment insights-client -n open-cluster-management &>/dev/null; then
    echo -n "ACM client query: "
    # Look in insights-client logs for any request that contains our app's service DNS name
    ACM_LOG=$(oc logs -n open-cluster-management deployment/insights-client --since=10m 2>/dev/null | grep "$APP.$NAMESPACE" | tail -1 || true)
    if [ -n "$ACM_LOG" ]; then
        echo "✓"
        $VERBOSE && echo "  $ACM_LOG"
    else
        # Not yet — insights-client polls on an interval, so it may not have queried yet
        echo "⚠️  (not yet)"
    fi
fi

# --- Check 6: Has the insights-operator downloaded a report? ---
echo -n "Insights report download: "
# `oc get insightsoperator cluster` — get the InsightsOperator custom resource (CR)
# that tracks the insights-operator's state including the last downloaded report.
# -o jsonpath: extract the insightsReport status section as a JSON string
INSIGHTS_REPORT=$(oc get insightsoperator cluster -o jsonpath='{.status.insightsReport}' 2>/dev/null || true)
if [ -n "$INSIGHTS_REPORT" ]; then
    # Extract the downloadedAt timestamp from the JSON string using grep + cut
    # grep -o: print only the matching part, not the whole line
    # cut -d'"' -f4: the 4th field when splitting by double-quote characters
    DOWNLOADED_AT=$(echo "$INSIGHTS_REPORT" | grep -o '"downloadedAt":"[^"]*"' | cut -d'"' -f4)
    # Count how many health check descriptions are in the report (each "description" key = 1 check)
    # wc -l: count lines; tr -d ' ': remove whitespace from the count
    HEALTH_COUNT=$(echo "$INSIGHTS_REPORT" | grep -o '"description"' | wc -l | tr -d ' ')
    # Show the result — non-empty downloadedAt means a report was downloaded
    [ -n "$DOWNLOADED_AT" ] && echo "✓ (${HEALTH_COUNT} healthChecks at ${DOWNLOADED_AT})" || echo "⚠️  (not downloaded yet)"
else
    echo "⚠️  (no report)"
fi

# --- Check 7: Does the upgrade-risks-prediction endpoint work? ---
echo -n "Upgrade risks prediction: "
# Get the cluster ID from the ClusterVersion resource — this is the unique UUID of this cluster.
# `oc get clusterversion version` refers to the ClusterVersion CR (always named "version").
# `.spec.clusterID` is the cluster's UUID.
CLUSTER_ID=$(oc get clusterversion version -o jsonpath='{.spec.clusterID}' 2>/dev/null || true)
if [ -n "$CLUSTER_ID" ]; then
    # Execute a Python one-liner INSIDE the running app container to call the prediction endpoint.
    # This tests the endpoint from inside the cluster network (avoiding any external routing issues).
    # `oc exec -n $NAMESPACE deployment/$APP -- python3 -c "..."` runs Python inside the pod.
    UPGRADE_RESULT=$(oc exec -n $NAMESPACE deployment/$APP -- python3 -c "
import urllib.request, json, urllib.error
# Build the JSON request body with the cluster ID
data = json.dumps({'cluster_id': '$CLUSTER_ID'}).encode()
# Create an HTTP POST request to the local upgrade-risks-prediction endpoint
req = urllib.request.Request('http://localhost:8000/upgrade-risks-prediction', data=data, headers={'Content-Type': 'application/json'})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    r = json.loads(resp.read())
    # Print the status and upgrade_recommended fields for easy grep-ing
    print(r.get('status',''), 'upgrade_recommended=' + str(r.get('upgrade_recommended','')))
except Exception as e:
    print('ERROR:', e)
" 2>/dev/null || true)
    # Check if the output contains "ok" (the expected status value on success)
    if echo "$UPGRADE_RESULT" | grep -q "ok"; then
        echo "✓ ($UPGRADE_RESULT)"
    else
        echo "❌ ($UPGRADE_RESULT)"
    fi
else
    # ClusterVersion resource not found — unusual, might not be on an OpenShift cluster
    echo "⚠️  (could not get cluster ID)"
fi

echo
# Summary line showing the expected data flow through the pipeline
echo "Pipeline: insights-operator → $APP → ACM insights-client"
echo "Upgrades: $APP → rbac-query-proxy → MCO Thanos"
# Remind the user about verbose mode if they're not already using it
if ! $VERBOSE; then
    echo
    echo "Tip: Run with -v or --verbose for detailed output"
fi
