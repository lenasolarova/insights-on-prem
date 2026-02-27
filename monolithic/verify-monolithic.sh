#!/bin/bash
# Monolithic Pipeline Verification Script

VERBOSE=false
if [[ "$1" == "-v" || "$1" == "--verbose" ]]; then
    VERBOSE=true
fi

NAMESPACE="insights-on-prem-poc"
APP="insights-on-prem"

echo "=== Monolithic Pipeline Verification ==="
echo

# Check app is running
echo -n "App running: "
PHASE=$(oc get pod -l app=$APP -n $NAMESPACE -o jsonpath='{.items[0].status.phase}' 2>/dev/null)
if [ "$PHASE" == "Running" ]; then
    echo "✓"
else
    echo "❌ ($PHASE)"
fi

# Check insights-operator upload
echo -n "Insights upload: "
UPLOAD_LOG=$(oc logs -n openshift-insights deployment/insights-operator --since=10m 2>/dev/null | grep "Uploaded report successfully" || true)
if [ -n "$UPLOAD_LOG" ]; then
    echo "✓"
    $VERBOSE && echo "  $UPLOAD_LOG"
else
    echo "⚠️  (wait ~2min after: oc delete pod -n openshift-insights -l app=insights-operator)"
fi

# Check monolithic app received the upload
echo -n "App received upload: "
RECV_LOG=$(oc logs -n $NAMESPACE deployment/$APP --since=10m 2>/dev/null | grep "POST /api/ingress/v1/upload" | grep "200\|202" | tail -1 || true)
if [ -n "$RECV_LOG" ]; then
    echo "✓"
    $VERBOSE && echo "  $RECV_LOG"
else
    echo "❌"
fi

# Check archive was processed (report stored)
echo -n "Archive processed: "
PROC_LOG=$(oc logs -n $NAMESPACE deployment/$APP --since=10m 2>/dev/null | grep "Successfully processed" | tail -1 || true)
if [ -n "$PROC_LOG" ]; then
    echo "✓"
    $VERBOSE && echo "  $PROC_LOG"
else
    echo "❌"
fi

# Check ACM insights-client is querying the monolithic app
if oc get deployment insights-client -n open-cluster-management &>/dev/null; then
    echo -n "ACM client query: "
    ACM_LOG=$(oc logs -n open-cluster-management deployment/insights-client --since=10m 2>/dev/null | grep "$APP.$NAMESPACE" | tail -1 || true)
    if [ -n "$ACM_LOG" ]; then
        echo "✓"
        $VERBOSE && echo "  $ACM_LOG"
    else
        echo "⚠️  (not yet)"
    fi
fi

# Check InsightsOperator report download
echo -n "Insights report download: "
INSIGHTS_REPORT=$(oc get insightsoperator cluster -o jsonpath='{.status.insightsReport}' 2>/dev/null || true)
if [ -n "$INSIGHTS_REPORT" ]; then
    DOWNLOADED_AT=$(echo "$INSIGHTS_REPORT" | grep -o '"downloadedAt":"[^"]*"' | cut -d'"' -f4)
    HEALTH_COUNT=$(echo "$INSIGHTS_REPORT" | grep -o '"description"' | wc -l | tr -d ' ')
    [ -n "$DOWNLOADED_AT" ] && echo "✓ (${HEALTH_COUNT} healthChecks at ${DOWNLOADED_AT})" || echo "⚠️  (not downloaded yet)"
else
    echo "⚠️  (no report)"
fi

# Check upgrade-risks-prediction endpoint
echo -n "Upgrade risks prediction: "
CLUSTER_ID=$(oc get insightsoperator cluster -o jsonpath='{.status.gatherStatus.gatherers[0].lastGatherDuration}' 2>/dev/null || true)
CLUSTER_ID=$(oc get clusterversion version -o jsonpath='{.spec.clusterID}' 2>/dev/null || true)
if [ -n "$CLUSTER_ID" ]; then
    UPGRADE_RESULT=$(oc exec -n $NAMESPACE deployment/$APP -- python3 -c "
import urllib.request, json, urllib.error
data = json.dumps({'cluster_id': '$CLUSTER_ID'}).encode()
req = urllib.request.Request('http://localhost:8000/upgrade-risks-prediction', data=data, headers={'Content-Type': 'application/json'})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    r = json.loads(resp.read())
    print(r.get('status',''), 'upgrade_recommended=' + str(r.get('upgrade_recommended','')))
except Exception as e:
    print('ERROR:', e)
" 2>/dev/null || true)
    if echo "$UPGRADE_RESULT" | grep -q "ok"; then
        echo "✓ ($UPGRADE_RESULT)"
    else
        echo "❌ ($UPGRADE_RESULT)"
    fi
else
    echo "⚠️  (could not get cluster ID)"
fi

echo
echo "Pipeline: insights-operator → $APP → ACM insights-client"
echo "Upgrades: $APP → rbac-query-proxy → MCO Thanos"
if ! $VERBOSE; then
    echo
    echo "Tip: Run with -v or --verbose for detailed output"
fi
