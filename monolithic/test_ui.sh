#!/bin/bash
# test_ui.sh - Sets up test data to verify all four Insights sections in the ACM fleet overview UI.
#
# Prerequisites: addon must be deployed (monolithic/addon/).
#
# Results are visible at: https://<your-cluster>/multicloud/home/overview

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UI_TESTS="$SCRIPT_DIR/tests/ui"
CLUSTER_ID=$(oc get clusterversion version -o jsonpath='{.spec.clusterID}')
NS="insights-on-prem"

# stolostron daily snapshot — has UPGRADE_RISKS_PREDICTION_URL support (CCXDEV-16237).
# MCH is paused via policy so this image sticks.
CONSOLE_IMAGE="quay.io/stolostron/console:latest-2.16"



echo "=== Insights On-Premise UI Test Setup ==="
echo "Cluster ID: $CLUSTER_ID"
echo ""

# ---------------------------------------------------------------------------
echo "1. Triggering cluster recommendations..."
# ---------------------------------------------------------------------------
oc apply -f "$UI_TESTS/webhook-trigger.yaml"
oc patch configs.samples.operator.openshift.io cluster --type merge -p '{"spec":{"managementState":"Unmanaged"}}'

# ---------------------------------------------------------------------------
echo ""
echo "2. Creating test data for update risk predictions and failing operators..."
# ---------------------------------------------------------------------------
oc apply -f "$UI_TESTS/critical-alerts.yaml"

# ---------------------------------------------------------------------------
echo ""
echo "3. Configuring on-prem service to query current Thanos data..."
# ---------------------------------------------------------------------------
oc set env deployment/insights-on-prem -n $NS THANOS_QUERY_LOOKBACK_MINUTES=0
oc rollout status deployment/insights-on-prem -n $NS --timeout=60s

# ---------------------------------------------------------------------------
echo ""
echo "4. Ensuring HTTPS route exists for console backend..."
# ---------------------------------------------------------------------------
oc create route edge insights-on-prem \
  -n $NS \
  --service=insights-on-prem \
  --port=8000 \
  --insecure-policy=Redirect 2>/dev/null || true

ON_PREM_ROUTE=$(oc get route insights-on-prem -n $NS -o jsonpath='{.spec.host}')
ON_PREM_URP_URL="https://${ON_PREM_ROUTE}/api/insights-results-aggregator/v2/upgrade-risks-prediction"
echo "   Route: $ON_PREM_URP_URL"

# ---------------------------------------------------------------------------
echo ""
echo "5. Ensuring console has stolostron snapshot image and URP URL..."
# ---------------------------------------------------------------------------
# MCH is paused via policy (insights-on-prem-mch-pause) so changes here stick.
# The stolostron snapshot already has UPGRADE_RISKS_PREDICTION_URL support (CCXDEV-16237).
oc set image deployment/console-chart-console-v2 -n open-cluster-management \
  console=$CONSOLE_IMAGE
oc set env deployment/console-chart-console-v2 -n open-cluster-management \
  UPGRADE_RISKS_PREDICTION_URL=$ON_PREM_URP_URL
oc rollout status deployment/console-chart-console-v2 -n open-cluster-management --timeout=120s

# ---------------------------------------------------------------------------
echo ""
echo "6. Waiting for alerts to reach Thanos (~2-5 min)..."
# ---------------------------------------------------------------------------
TOKEN=$(oc exec deployment/insights-on-prem -n $NS -- cat /var/run/secrets/kubernetes.io/serviceaccount/token)
for _ in $(seq 1 10); do
  COUNT=$(oc exec deployment/insights-on-prem -n $NS -- sh -c \
    "curl -sk -H 'Authorization: Bearer $TOKEN' \
     'https://rbac-query-proxy.open-cluster-management-observability.svc.cluster.local:8443/api/v1/query' \
     --data-urlencode 'query=ALERTS{alertname=~\"InsightsTest.*\"}' 2>/dev/null" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['result']))" 2>/dev/null)
  echo "   $(date '+%H:%M:%S') alerts in Thanos: ${COUNT:-0}"
  [ "${COUNT:-0}" -gt 0 ] && break
  sleep 30
done

# ---------------------------------------------------------------------------
echo ""
echo "7. Verifying URP data comes from on-prem via the actual console route..."
# ---------------------------------------------------------------------------
URP_RESULT=$(curl -sk -X POST "$ON_PREM_URP_URL" \
  -H 'Content-Type: application/json' \
  -d "{\"clusters\": [\"$CLUSTER_ID\"]}" 2>/dev/null)

HAS_ALERTS=$(echo "$URP_RESULT" | grep -c "InsightsTestCriticalAlert" || true)
UPGRADE_RECOMMENDED=$(echo "$URP_RESULT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); p=d.get('predictions',[]); print(p[0].get('upgrade_recommended','?') if p else '?')" 2>/dev/null)

PASS=0; FAIL=0
check() {
  if [ "$2" = "ok" ]; then echo "  [PASS] $1"; PASS=$((PASS+1))
  else echo "  [FAIL] $1 — $2"; FAIL=$((FAIL+1)); fi
}

check "batch URP endpoint returns cluster-local fake alerts via HTTPS route (proves full path works)" \
  "$([ "${HAS_ALERTS:-0}" -gt 0 ] && echo ok || echo "alerts not found - Thanos may need more time")"
check "batch URP endpoint returns upgrade_recommended=False" \
  "$([ "$UPGRADE_RECOMMENDED" = "False" ] && echo ok || echo "got: $UPGRADE_RECOMMENDED")"

echo ""
echo "Results: $PASS passed, $FAIL failed"
echo ""
echo "=== Done ==="
echo "Check the UI at: https://$(oc get infrastructure cluster -o jsonpath='{.status.apiServerURL}' | sed 's|https://api\.|console-openshift-console.apps.|' | sed 's|:6443||')/multicloud/home/overview"
echo ""
echo "To clean up:"
echo "  oc delete validatingwebhookconfiguration insights-test-webhook"
echo "  oc patch configs.samples.operator.openshift.io cluster --type merge -p '{\"spec\":{\"managementState\":\"Managed\"}}'"
echo "  oc delete prometheusrule insights-test-alerts -n openshift-monitoring"
echo "  oc delete route insights-on-prem -n $NS"
