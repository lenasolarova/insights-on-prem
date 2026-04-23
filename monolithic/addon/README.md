# Insights On-Prem ACM Addon

Deploys the Insights on-premise monolithic app to the ACM hub cluster (`local-cluster`) as a managed addon. Once deployed, all Insights data flows — archive uploads, recommendations, and upgrade risk predictions — are routed to the on-prem service instead of `console.redhat.com`.

## Prerequisites

- OpenShift 4.x on AWS
- ACM 2.16+ with MultiClusterHub in `Running` state
- MCO deployed (see below) — required for Thanos metrics used by upgrade risk predictions
- Pull secrets for the two private `quay.io/ccxdev` images (see below)

## Pull secrets

Two images require credentials for `quay.io/ccxdev` — one for the app, one for the patched console.
Both use the same robot account (`ccxdev+insights_on_prem_poc`). The secret files are gitignored;
fill in credentials before deploying:

```
monolithic/addon/ccxdev-insights-on-prem-poc-secret.yml      # namespace: insights-on-prem
monolithic/addon/ccxdev-insights-on-prem-poc-secret-ocm.yml  # namespace: open-cluster-management
```

## Setting up MCO

Run from a clone of `stolostron/multicluster-observability-operator`:

```bash
kubectl create ns open-cluster-management-observability

# MinIO as the S3-compatible object storage backend (dev/testing only)
kubectl -n open-cluster-management-observability apply -k examples/minio

# Deploy the MCO CR — triggers Thanos receiver, store, query, compactor, alertmanager
kubectl apply -f operators/multiclusterobservability/config/samples/observability_v1beta2_multiclusterobservability.yaml

# Verify all pods come up healthy
kubectl -n open-cluster-management-observability get pod
```

## Install

```bash
cd monolithic/addon/

# 1. Namespaces — pre-creates both hub and workload namespaces so pull secrets can be applied immediately
kubectl apply -f 01-namespace.yaml

# 2. Pull secrets (fill in credentials first — see above)
kubectl apply -f ccxdev-insights-on-prem-poc-secret.yml        # insights-on-prem ns
kubectl apply -f ccxdev-insights-on-prem-poc-secret-ocm.yml    # open-cluster-management ns

# 3. Addon resources (order matters)
kubectl apply -f 04-addon-template.yaml
kubectl apply -f 02-addon.yaml
kubectl apply -f 03-placement.yaml
kubectl apply -f 05-policies.yaml
```

> The addon manager automatically creates the `ManagedClusterAddOn` on `local-cluster` based on the Placement — do not apply `05-managedclusteraddon.yaml` manually.

The configure job completes in ~30 seconds and insights-operator restarts to pick up the redirect config. All other settings (console image, `CCX_SERVER`, `UPGRADE_RISKS_PREDICTION_URL`, MCH pause) are enforced continuously by ConfigurationPolicies and survive pod restarts.

## What gets deployed

### Workload (via AddOnTemplate → ManifestWork on local-cluster)

| Resource | Namespace | Purpose |
|---|---|---|
| Namespace | `insights-on-prem` | Workload namespace |
| PostgreSQL Deployment + PVC | `insights-on-prem` | Persistent database |
| insights-on-prem Deployment | `insights-on-prem` | FastAPI app (port 8000) |
| Route (edge TLS) | `insights-on-prem` | External HTTPS access for console URP calls |
| ConfigMap `insights-config` | `openshift-insights` | Redirects insights-operator uploads + report downloads to on-prem |
| Configure Job | `insights-on-prem` | Restarts insights-operator to pick up the redirect config |

### Policies (enforced continuously, survive pod restarts)

| Policy | What it enforces |
|---|---|
| `insights-on-prem-mch-pause` | Pauses MCH so it doesn't revert the console image or other settings |
| `insights-on-prem-console` | Pins console to patched image, sets `imagePullSecrets`, sets `UPGRADE_RISKS_PREDICTION_URL` dynamically from the route hostname |
| `insights-on-prem-insights-client` | Sets `CCX_SERVER` on the `insights-client` deployment to redirect recommendations to on-prem |

## Traffic flow

Once deployed, all Insights traffic flows to the on-prem service:

| Source | Endpoint | What it does |
|---|---|---|
| insights-operator | `http://insights-on-prem.insights-on-prem.svc:8000/api/ingress/v1/upload` | Uploads cluster archive |
| insights-operator | `http://insights-on-prem.insights-on-prem.svc:8000/api/v2/cluster/<id>/reports` | Downloads processed recommendations |
| insights-client | `http://insights-on-prem.insights-on-prem.svc:8000/api/v2` (CCX_SERVER) | Fetches recommendations for ACM UI |
| ACM console backend | `https://<route>/api/insights-results-aggregator/v2/upgrade-risks-prediction` | Fetches upgrade risk predictions for fleet overview |

> **Note:** insights-operator makes one call to `console.redhat.com/api/gathering/v2/.../gathering_rules` to fetch what data to collect. This is a separate config field (`conditionalGathererEndpoint`) not covered by `insights-config` and does not send any cluster data externally.

## Verify

```bash
# Addon status (should show Available: True)
kubectl get managedclusteraddon insights-on-prem -n local-cluster

# All workload pods running
kubectl get pod -n insights-on-prem

# Configure job completed
kubectl logs -n insights-on-prem job/insights-on-prem-configure

# All policies Compliant
kubectl get policy -n insights-on-prem

# On-prem service receiving traffic
kubectl logs -n insights-on-prem deployment/insights-on-prem --since=5m | grep "INFO:"
```

## UI tests

Once the configure job completes (~30s after addon deploys), run the test script to inject test data and verify the full end-to-end flow:

```bash
cd monolithic/
bash test_ui.sh
```

This creates test recommendations and alerts, waits for them to reach Thanos, and verifies the URP endpoint returns the correct data. Check the ACM fleet overview at `https://<your-cluster>/multicloud/home/overview` — you should see both Recommendations and Upgrade Risk Predictions populated.

## Console image — temporary requirement

The addon enforces `quay.io/ccxdev/insights-on-prem-lsolarov-console:latest` via the `insights-on-prem-console` policy. This is a patched build of `stolostron/console:latest-2.16` that supports the `UPGRADE_RISKS_PREDICTION_URL` env var. The upstream fix is tracked in [CCXDEV-16237](https://redhat.atlassian.net/browse/CCXDEV-16237) and a PR is pending on `stolostron/console`.

**Once the upstream fix lands in an ACM release**, remove:
- The `insights-on-prem-console` policy entries for `image` and `imagePullSecrets` (keep `UPGRADE_RISKS_PREDICTION_URL`)
- Both `ccxdev-insights-on-prem-poc-secret*.yml` pull secret files
- Rebuild instructions below are no longer needed

**To rebuild the custom image** (e.g. after a new ACM 2.16 patch release):

```bash
# 1. Extract the current 2.16 backend.mjs from a running console pod
kubectl cp open-cluster-management/<console-pod>:/app/backend.mjs /tmp/backend-2.16.mjs

# 2. Apply the || fix
python3 -c "
path='/tmp/backend-2.16.mjs'
c=open(path).read()
c=c.replace(
  \"insightsPath = 'https://console.redhat.com/api/insights-results-aggregator/v2/upgrade-risks-prediction'\",
  \"insightsPath = process.env.UPGRADE_RISKS_PREDICTION_URL || 'https://console.redhat.com/api/insights-results-aggregator/v2/upgrade-risks-prediction'\", 1)
open(path,'w').write(c)"

# 3. Build and push (must use latest-2.16 as base — main branch has TLS watcher incompatible with 2.16 RBAC)
cat > /tmp/Dockerfile.patch << 'EOF'
FROM quay.io/stolostron/console:latest-2.16
COPY backend-2.16.mjs /app/backend.mjs
EOF
docker build --platform linux/amd64 -t quay.io/ccxdev/insights-on-prem-lsolarov-console:latest \
  -f /tmp/Dockerfile.patch /tmp/
docker push quay.io/ccxdev/insights-on-prem-lsolarov-console:latest
```

## Uninstall

```bash
kubectl delete -f 05-policies.yaml
kubectl delete -f 03-placement.yaml
kubectl delete -f 02-addon.yaml
kubectl delete -f 04-addon-template.yaml
kubectl delete -f 01-namespace.yaml
```
