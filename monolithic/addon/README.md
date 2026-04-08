# Insights On-Premise ACM Addon

ACM addon that deploys the on-prem service and continuously reconciles cluster configuration.

## How it works

`04-addon-template.yaml` → ManifestWork → deploys the pod, service, HTTPS route, RBAC, `insights-config` ConfigMap.

`05-policies.yaml` → ConfigurationPolicies → enforce and restore on drift:
- MCH pause *(temporary — MCH still hardcodes `CCX_SERVER`)*
- `CCX_SERVER` + `POLL_INTERVAL` on `insights-client`
- Console image pinned to `latest-2.16` *(temporary — CCXDEV-16237)*
- `UPGRADE_RISKS_PREDICTION_URL` on the console (internal cluster DNS, no cluster-specific value)

## Files

| File | What it does |
|------|--------------|
| `01-namespace.yaml` | Namespace for addon resources + `ManagedClusterSetBinding` |
| `02-addon.yaml` | `ClusterManagementAddOn` |
| `03-placement.yaml` | Targets `local-cluster` (hub) only |
| `04-addon-template.yaml` | Static manifests via ManifestWork (`CreateOnly` for the deployment — external changes are not reverted) |
| `05-policies.yaml` | ConfigurationPolicies for CCX_SERVER, console image, URP URL, MCH pause |

## Prerequisites

- ACM installed with MCH in `Running` state
- MCO deployed (required for Thanos/URP) — [setup instructions](https://github.com/stolostron/multicluster-observability-operator/tree/main?tab=readme-ov-file#run-the-operator-in-the-cluster)

## Install

```bash
oc apply -f monolithic/addon/
oc apply -f monolithic/deploy/ccxdev-insights-on-prem-poc-secret.yml -n insights-on-prem-poc
oc create secret generic search-postgres -n insights-on-prem-poc \
  --from-literal=database-user="$(oc get secret search-postgres -n open-cluster-management -o jsonpath='{.data.database-user}' | base64 -d)" \
  --from-literal=database-password="$(oc get secret search-postgres -n open-cluster-management -o jsonpath='{.data.database-password}' | base64 -d)" \
  --from-literal=database-name="$(oc get secret search-postgres -n open-cluster-management -o jsonpath='{.data.database-name}' | base64 -d)"
```

Then run `test_ui.sh` to set up test data and verify the UI.

### Hub-of-hubs workaround

If `oc get klusterlet klusterlet -o jsonpath='{.spec.clusterName}'` differs from the `ManagedCluster` name (`local-cluster`), neither ManifestWork nor ConfigurationPolicies will enforce — the klusterlet watches the wrong namespace, and the governance framework talks to the outer hub rather than the local one. Apply everything manually after `oc apply -f monolithic/addon/`:

```bash
# Pod-level resources (normally applied via ManifestWork)
oc apply -f monolithic/addon/resources/

# Secrets (namespace now exists from above)
oc apply -f monolithic/deploy/ccxdev-insights-on-prem-poc-secret.yml -n insights-on-prem-poc
oc create secret generic search-postgres -n insights-on-prem-poc \
  --from-literal=database-user="$(oc get secret search-postgres -n open-cluster-management -o jsonpath='{.data.database-user}' | base64 -d)" \
  --from-literal=database-password="$(oc get secret search-postgres -n open-cluster-management -o jsonpath='{.data.database-password}' | base64 -d)" \
  --from-literal=database-name="$(oc get secret search-postgres -n open-cluster-management -o jsonpath='{.data.database-name}' | base64 -d)"

# Configuration (normally enforced via ConfigurationPolicies)
oc annotate multiclusterhub multiclusterhub -n open-cluster-management 'installer.open-cluster-management.io/pause=true' --overwrite
oc set env deployment/insights-client -n open-cluster-management \
  CCX_SERVER=http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000/api/v2 POLL_INTERVAL=1
oc set image deployment/console-chart-console-v2 -n open-cluster-management console=quay.io/stolostron/console:latest-2.16
oc set env deployment/console-chart-console-v2 -n open-cluster-management \
  UPGRADE_RISKS_PREDICTION_URL=http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000/api/insights-results-aggregator/v2/upgrade-risks-prediction
oc rollout restart deployment/insights-operator -n openshift-insights
```

## Uninstall

```bash
oc annotate multiclusterhub multiclusterhub -n open-cluster-management installer.open-cluster-management.io/pause-
oc set env deployment/insights-client -n open-cluster-management CCX_SERVER- POLL_INTERVAL-
oc set env deployment/console-chart-console-v2 -n open-cluster-management UPGRADE_RISKS_PREDICTION_URL-
oc delete -f monolithic/addon/
oc delete namespace insights-on-prem-poc
```

## Known limitations

- **PostgreSQL**: temporarily borrows `search-postgres` from ACM search — needs its own DB
- **insights-client image**: pinned by digest in `05-policies.yaml` (ConfigurationPolicy requires `image` in container spec) — update when MCH upgrades it
- **MCH pause**: needed on ACM 2.16.0 because MCH still hardcodes `CCX_SERVER` on insights-client — remove once MCH ships a version that doesn't
- **Hub-of-hubs mismatch**: when the klusterlet cluster name differs from the `ManagedCluster` name, ManifestWork goes to `local-cluster` namespace (klusterlet doesn't watch it) and ConfigurationPolicies don't enforce (governance framework is wired to the outer hub, not this one) — see workaround above
