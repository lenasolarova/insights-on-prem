#!/bin/bash
# deploy.sh — Full deployment script for the Insights On-Premise POC
# ====================================================================
# This script automates the complete setup of the monolithic on-premise service
# on an OpenShift cluster. It uses `oc` (the OpenShift CLI) to apply Kubernetes
# manifests and configure existing system components to route through this service.
#
# Prerequisites:
#   - oc CLI installed and logged into the target OpenShift cluster
#   - The search-postgres secret exists in open-cluster-management namespace
#   - The ccxdev-insights-on-prem-poc-secret.yml pull secret file exists locally
#
# What this script does (in order):
#   1. Creates the deployment namespace
#   2. Copies the PostgreSQL credentials secret into the new namespace
#   3. Applies the image pull secret so the pod can pull from quay.io
#   4. Creates a ServiceAccount with cluster-reader permissions for Thanos access
#   5. Deploys the application pod
#   6. Creates a Kubernetes Service (internal DNS name) for the pod
#   7. Reconfigures insights-operator to upload archives to our service
#   8. Pauses the MultiClusterHub operator so it can't undo our changes
#   9. Reconfigures ACM insights-client to query our service for reports
#  10. Waits for the deployment rollout to complete

# set -e: exit immediately if any command fails with a non-zero exit code.
# Without this, the script would continue even after a failed `oc apply`, which
# could leave things in a broken half-deployed state.
set -e

echo "=== Deploying Insights On-Premise POC ==="

# Step 1: Create the Kubernetes namespace where all our resources will live.
# `oc apply -f` creates or updates a resource from a YAML manifest file.
# The namespace is defined in deploy/namespace.yml as "insights-on-prem-poc".
echo "1. Creating namespace..."
oc apply -f deploy/namespace.yml

# Step 2: Copy the PostgreSQL database credentials from the ACM namespace into ours.
#
# The PostgreSQL instance used is the same one that ACM's search component uses.
# Rather than maintaining separate credentials, we borrow the existing secret.
#
# `oc get secret search-postgres -n open-cluster-management -o json` — exports the secret as JSON
# `jq 'del(...)'` — strips Kubernetes-managed metadata fields that would cause an error
#                   if we tried to apply a secret with an existing resourceVersion/uid
# `.metadata.namespace = "insights-on-prem-poc"` — reassigns the secret to our namespace
# `oc apply --namespace insights-on-prem-poc -f -` — creates/updates it in our namespace
echo "2. Copying PostgreSQL secret..."
oc get secret search-postgres -n open-cluster-management -o json | \
  jq 'del(.metadata.namespace, .metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp, .metadata.ownerReferences) | .metadata.namespace = "insights-on-prem-poc"' | \
  oc apply --namespace insights-on-prem-poc -f -

# Step 3: Apply the image pull secret so Kubernetes can pull our private container image
# from quay.io. Without this, the pod would fail to start with an "ImagePullBackOff" error.
echo "3. Applying secrets..."
oc apply -f deploy/ccxdev-insights-on-prem-poc-secret.yml --namespace insights-on-prem-poc

# Step 4: Create the ServiceAccount and bind it to the cluster-reader ClusterRole.
# The ServiceAccount is what the pod's containers run as within Kubernetes.
# The cluster-reader role allows it to read cluster-wide resources (needed to query Thanos).
# The serviceaccount.yml also creates a ClusterRoleBinding linking them.
echo "4. Setting up ServiceAccount for Thanos access..."
oc apply -f deploy/serviceaccount.yml

# Step 5: Deploy the application using the Deployment manifest.
# The Deployment manages the pod lifecycle (creating, restarting, scaling the pod).
# It references the ServiceAccount and container image defined in deploy/insights.yml.
echo "5. Deploying application..."
oc apply -f deploy/insights.yml --namespace insights-on-prem-poc

# Step 6: Create the Kubernetes Service resource.
# A Service gives the pod a stable internal DNS name and virtual IP.
# Other pods in the cluster can reach our app at:
#   http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000
echo "6. Creating service..."
oc apply -f deploy/service.yml --namespace insights-on-prem-poc

# Step 7: Reconfigure the OpenShift insights-operator to send uploads to our service.
#
# By default, insights-operator uploads to Red Hat's cloud ingress endpoint.
# deploy/insights-config.yml contains a ConfigMap that overrides this URL to point
# to our local service instead. This means all archive uploads go to us, not the cloud.
#
# After applying the ConfigMap, we restart the operator deployment so it picks up
# the new configuration immediately (pods continue running with old config until restarted).
echo "7. Configuring OpenShift insights-operator..."
# Apply insights-operator ConfigMap to redirect uploads to on-premise service
oc apply -f deploy/insights-config.yml
oc rollout restart -n openshift-insights deployment insights-operator

# Step 8: Pause the MultiClusterHub (MCH) operator so it doesn't undo our changes.
#
# MCH is the operator that manages all ACM components (including insights-client).
# When MCH reconciles its state, it would reset any manual changes we make to
# deployments it manages. The `mch-pause=true` annotation tells MCH to stop reconciling,
# giving us a stable window to make and keep our changes.
#
# IMPORTANT: Remember to un-pause MCH when done testing:
#   oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause-
echo "8. Pausing MultiClusterHub operator..."
# Pause the operator to prevent it from reverting our changes in insights-client deployment
oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause=true --overwrite

# Step 9: Reconfigure ACM's insights-client to query our service for cluster reports.
#
# The CCX_SERVER environment variable tells insights-client where to fetch insights results.
# We change it from the Red Hat cloud endpoint to our in-cluster service URL.
# `/api/v2` — our service exposes the v2 report API at this path prefix.
#
# We also speed up the polling interval (POLL_INTERVAL=1 minute) for demo/testing purposes.
# In production this would typically be much longer (hours).
echo "9. Configuring ACM insights-client..."
# Update the CCX_SERVER environment variable to point to on-premise service
oc set env deployment/insights-client -n open-cluster-management \
  CCX_SERVER=http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000/api/v2

# Set insights-client poll interval to 1 minute for demo purposes
oc set env deployment/insights-client -n open-cluster-management \
  POLL_INTERVAL=1

# Step 10: Wait for the insights-client deployment to finish rolling out.
# `oc rollout status` blocks until all pods in the deployment are running and ready,
# or until the --timeout expires (120 seconds here).
echo "10. Waiting for deployment to roll out..."
oc rollout status deployment/insights-client -n open-cluster-management --timeout=120s

echo ""
echo "=== Deployment Complete ==="
echo ""
# Remind the operator about the paused MCH — this is important to remember!
echo "IMPORTANT: MultiClusterHub operator is PAUSED (mch-pause=true annotation)"
echo "           This prevents the operator from reverting the CCX_SERVER change."
echo "           To unpause: oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause-"
echo ""
# Provide a quick command to verify the CCX_SERVER was set correctly
echo "To verify insights-client configuration:"
echo "  oc get deployment insights-client -n open-cluster-management -o yaml | grep -A2 'name: CCX_SERVER'"
