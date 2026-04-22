# Insights On-Prem ACM Addon

Deploys the Insights on-premise monolithic app to the ACM hub cluster (`local-cluster`) as a managed addon.

## Prerequisites

- OpenShift 4.x on AWS
- ACM 2.16+ with MultiClusterHub in `Running` state
- MCO deployed (see below)
- Pull secrets for the two private quay.io images (see below)

## Pull secrets

Two images require a pull secret for `quay.io/ccxdev` — one for the app, one for the console.
Both use the same robot account. Fill in the credentials in the two secret files before deploying:

```
monolithic/addon/ccxdev-insights-on-prem-poc-secret.yml      # namespace: insights-on-prem
monolithic/addon/ccxdev-insights-on-prem-poc-secret-ocm.yml  # namespace: open-cluster-management
```

Both files are gitignored. Robot account name: `ccxdev+insights_on_prem_poc`.

## Setting up MCO

Run from a clone of `stolostron/multicluster-observability-operator`:

```bash
kubectl create ns open-cluster-management-observability

# MinIO as the object storage backend (dev/testing only)
kubectl -n open-cluster-management-observability apply -k examples/minio

# Deploy the MCO CR
kubectl apply -f operators/multiclusterobservability/config/samples/observability_v1beta2_multiclusterobservability.yaml

# Verify — expect to see Thanos receiver, store, query, compactor, alertmanager pods
kubectl -n open-cluster-management-observability get pod
```

## Install

```bash
cd monolithic/addon/

# Pull secrets (fill in credentials first)
kubectl apply -f ccxdev-insights-on-prem-poc-secret.yml
kubectl apply -f ccxdev-insights-on-prem-poc-secret-ocm.yml

# Addon resources (order matters)
kubectl apply -f 01-namespace.yaml
kubectl apply -f 04-addon-template.yaml
kubectl apply -f 02-addon.yaml
kubectl apply -f 03-placement.yaml
kubectl apply -f 05-policies.yaml
```

> The addon manager automatically creates the `ManagedClusterAddOn` on `local-cluster` — do not apply `05-managedclusteraddon.yaml` manually.

## What gets deployed

| Resource | Namespace | Purpose |
|---|---|---|
| Namespace | `insights-on-prem` | Workload namespace |
| PostgreSQL Deployment + PVC | `insights-on-prem` | Persistent database |
| insights-on-prem Deployment | `insights-on-prem` | FastAPI app (port 8000) |
| Route (edge TLS) | `insights-on-prem` | External HTTPS access |
| ConfigMap `insights-config` | `openshift-insights` | Redirects insights-operator to on-prem |
| Configure Job | `insights-on-prem` | Redirects insights-client, patches console URL, restarts insights-operator |
| ConfigurationPolicy (console image) | hub | Pins console to build with `UPGRADE_RISKS_PREDICTION_URL` support |
| ConfigurationPolicy (MCH pause) | hub | Prevents MCH from reverting console image |

## Verify

```bash
# Addon status
kubectl get managedclusteraddon insights-on-prem -n local-cluster

# Workload
kubectl get deploy,svc,route -n insights-on-prem

# Configure job logs
kubectl logs -n insights-on-prem job/insights-on-prem-configure

# Policy compliance
kubectl get policy -n insights-on-prem
```

## UI tests

Once the configure job completes, run the UI test script to inject test data and verify end-to-end:

```bash
cd monolithic/
bash test_ui.sh
```

Check results at: `https://<your-cluster>/multicloud/home/overview`

## Notes on the console image

The addon enforces `quay.io/ccxdev/insights-on-prem-lsolarov-console:latest` via a ConfigurationPolicy. This is a patched build of `stolostron/console:latest-2.16` with a one-line fix (`??` → `||` in `upgrade-risks-prediction.ts`) that makes `UPGRADE_RISKS_PREDICTION_URL` readable at runtime. The upstream PR is tracked in [CCXDEV-16237](https://redhat.atlassian.net/browse/CCXDEV-16237) / [stolostron/console#5892](https://github.com/stolostron/console/pull/5892). Once a new ACM release ships with this fix, the console image policy and its pull secret can be removed.

## Uninstall

```bash
kubectl delete -f 05-policies.yaml
kubectl delete -f 03-placement.yaml
kubectl delete -f 02-addon.yaml
kubectl delete -f 04-addon-template.yaml
kubectl delete -f 01-namespace.yaml
```
