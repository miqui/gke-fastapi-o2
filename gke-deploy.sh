#!/usr/bin/env bash
#
# gke-deploy.sh — provision a dev GKE cluster on Google Cloud.
#
# Architecture:
#   - Zonal cluster (no HA): 1 managed control plane, e2-medium workers
#   - Node autoscaling MIN_NODES..MAX_NODES (default 2..5)
#   - Dedicated VPC + subnet with Private Google Access
#   - Private nodes (no external IPs); control plane locked to operator's IP
#   - Cloud Router + Cloud NAT for node/pod egress (cloudflared, packages)
#   - Artifact Registry repo in-region with image streaming (pulls bypass NAT, free)
#
# Usage:
#   PROJECT_ID=my-project ./gke-deploy.sh
#
# Overridable env vars: REGION, ZONE, CLUSTER, VPC, SUBNET, ROUTER, NAT,
#                       REPO, MACHINE_TYPE, MIN_NODES, MAX_NODES
set -euo pipefail
trap 'echo "ERROR: failed at line $LINENO (exit $?)" >&2' ERR

# ---- Config ---------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:?PROJECT_ID is required, e.g. PROJECT_ID=my-project ./gke-deploy.sh}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER="${CLUSTER:-dev-cluster}"
VPC="${VPC:-dev-vpc}"
SUBNET="${SUBNET:-dev-subnet}"
ROUTER="${ROUTER:-dev-router}"
NAT="${NAT:-dev-nat}"
REPO="${REPO:-api-images}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-medium}"
MIN_NODES="${MIN_NODES:-2}"
MAX_NODES="${MAX_NODES:-5}"

SUBNET_RANGE="10.0.0.0/20"
PODS_RANGE="10.4.0.0/14"
SERVICES_RANGE="10.8.0.0/20"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
skip() { echo "    already exists, skipping"; }

command -v gcloud >/dev/null 2>&1 || { echo "gcloud CLI not found" >&2; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl not found" >&2; exit 1; }

# ---- 0. Project setup -----------------------------------------------------
log "Setting project: $PROJECT_ID"
gcloud config set project "$PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# ---- 0b. Preflight: verify effective IAM permissions ------------------------
# Uses resourcemanager.testIamPermissions, which accounts for permissions
# inherited from the org, folders, and group memberships (not just direct
# project bindings). Skip with IAM_CHECK=0.
IAM_CHECK="${IAM_CHECK:-1}"
if [[ "$IAM_CHECK" == "1" ]]; then
  ACCOUNT=$(gcloud config get-value account)
  log "Checking IAM permissions for $ACCOUNT"

  REQUIRED_PERMS=(
    resourcemanager.projects.get
    serviceusage.services.enable
    compute.networks.create
    compute.subnetworks.create
    compute.routers.create
    container.clusters.create
    container.clusters.getCredentials
    artifactregistry.repositories.create
    artifactregistry.repositories.setIamPolicy
  )

  PERMS_JSON=$(printf '"%s",' "${REQUIRED_PERMS[@]}")
  RESPONSE=$(curl -s --max-time 15 -X POST \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -d "{\"permissions\":[${PERMS_JSON%,}]}" \
    "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}:testIamPermissions" || true)

  if [[ -z "$RESPONSE" || "$RESPONSE" == *'"error"'* ]]; then
    echo "ERROR: IAM permission check failed (does $ACCOUNT have any access to '$PROJECT_ID'?):" >&2
    echo "${RESPONSE:-<empty response>}" >&2
    exit 1
  fi

  MISSING=""
  for perm in "${REQUIRED_PERMS[@]}"; do
    [[ "$RESPONSE" == *"\"$perm\""* ]] || MISSING="${MISSING}  - ${perm}"$'\n'
  done

  if [[ -n "$MISSING" ]]; then
    cat >&2 <<EOF
ERROR: $ACCOUNT is missing required permissions:

$MISSING
Grant them (predefined roles cover all of the above), then re-run:

  gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$ACCOUNT" --role=roles/compute.admin
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$ACCOUNT" --role=roles/container.admin
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$ACCOUNT" --role=roles/artifactregistry.admin
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$ACCOUNT" --role=roles/serviceusage.admin

(Or simply roles/owner on a dev project.)
If permissions come from a source this check can't see, skip with IAM_CHECK=0.
EOF
    exit 1
  fi
  echo "    all required permissions present"
fi

log "Enabling APIs (container, compute, artifactregistry)"
gcloud services enable \
  container.googleapis.com \
  compute.googleapis.com \
  artifactregistry.googleapis.com

# ---- 1. Dedicated VPC + subnet --------------------------------------------
log "Creating VPC: $VPC"
if gcloud compute networks describe "$VPC" &>/dev/null; then
  skip
else
  gcloud compute networks create "$VPC" --subnet-mode=custom
fi

log "Creating subnet: $SUBNET ($SUBNET_RANGE, pods=$PODS_RANGE, services=$SERVICES_RANGE)"
if gcloud compute networks subnets describe "$SUBNET" --region="$REGION" &>/dev/null; then
  skip
else
  gcloud compute networks subnets create "$SUBNET" \
    --network="$VPC" \
    --region="$REGION" \
    --range="$SUBNET_RANGE" \
    --secondary-range="pods=$PODS_RANGE,services=$SERVICES_RANGE" \
    --enable-private-ip-google-access
fi

# ---- 2. Cloud Router + NAT (egress for private nodes) ----------------------
log "Creating Cloud Router: $ROUTER"
if gcloud compute routers describe "$ROUTER" --region="$REGION" &>/dev/null; then
  skip
else
  gcloud compute routers create "$ROUTER" --network="$VPC" --region="$REGION"
fi

log "Creating Cloud NAT: $NAT"
if gcloud compute routers nats describe "$NAT" --router="$ROUTER" --region="$REGION" &>/dev/null; then
  skip
else
  gcloud compute routers nats create "$NAT" \
    --router="$ROUTER" \
    --region="$REGION" \
    --auto-allocate-nat-external-ips \
    --nat-all-subnet-ip-ranges
fi

# ---- 3. Control-plane access restricted to operator's IP -------------------
log "Detecting operator public IP for master authorized networks"
MY_IP=$(curl -4 -s --max-time 10 ifconfig.me || true)
[[ "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "ERROR: could not detect public IP (got: '${MY_IP:-empty}')" >&2; exit 1;
}
echo "    operator IP: $MY_IP"

# ---- 4. GKE cluster ---------------------------------------------------------
log "Creating cluster: $CLUSTER (zonal $ZONE, $MACHINE_TYPE, autoscaling $MIN_NODES..$MAX_NODES, private nodes)"
if gcloud container clusters describe "$CLUSTER" --zone="$ZONE" &>/dev/null; then
  skip
else
  gcloud container clusters create "$CLUSTER" \
    --zone="$ZONE" \
    --network="$VPC" \
    --subnetwork="$SUBNET" \
    --enable-ip-alias \
    --cluster-secondary-range-name=pods \
    --services-secondary-range-name=services \
    --enable-private-nodes \
    --enable-master-authorized-networks \
    --master-authorized-networks="$MY_IP/32" \
    --machine-type="$MACHINE_TYPE" \
    --num-nodes="$MIN_NODES" \
    --enable-autoscaling \
    --min-nodes="$MIN_NODES" \
    --max-nodes="$MAX_NODES" \
    --release-channel=stable
fi

# ---- 5. Artifact Registry ---------------------------------------------------
log "Creating Artifact Registry repo: $REPO ($REGION)"
if gcloud artifacts repositories describe "$REPO" --location="$REGION" &>/dev/null; then
  skip
else
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker \
    --location="$REGION"
fi

log "Granting image pull access to node service account"
gcloud artifacts repositories add-iam-policy-binding "$REPO" \
  --location="$REGION" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/artifactregistry.reader"

log "Configuring local docker for push"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# ---- 6. Connect + verify -----------------------------------------------------
log "Fetching kubeconfig for $CLUSTER"
gcloud container clusters get-credentials "$CLUSTER" --zone="$ZONE"

log "Verifying cluster"
kubectl get nodes -o wide
kubectl cluster-info

# ---- Summary -----------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32mDone.\033[0m') Cluster '$CLUSTER' is ready.

  Push an image:
    docker tag my-api:v1 ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/my-api:v1
    docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/my-api:v1

  Reference it in a pod spec:
    image: ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/my-api:v1

  If your public IP changes, re-authorize it:
    gcloud container clusters update $CLUSTER --zone=$ZONE \\
      --enable-master-authorized-networks \\
      --master-authorized-networks=NEW_IP/32

  Resize node pool manually (autoscaler also manages this):
    gcloud container clusters resize $CLUSTER --zone=$ZONE --num-nodes=3

  Tear everything down:
    gcloud container clusters delete $CLUSTER --zone=$ZONE
    gcloud artifacts repositories delete $REPO --location=$REGION
    gcloud compute routers nats delete $NAT --router=$ROUTER --region=$REGION
    gcloud compute routers delete $ROUTER --region=$REGION
    gcloud compute networks subnets delete $SUBNET --region=$REGION
    gcloud compute networks delete $VPC
EOF
