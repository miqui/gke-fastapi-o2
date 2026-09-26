#!/usr/bin/env bash
#
# gke-teardown.sh — delete everything created by gke-deploy.sh, in dependency order:
#   1. GKE cluster   (also removes its nodes, firewalls, and managed LB resources)
#   2. Artifact Registry repo (and all images in it)
#   3. Cloud NAT
#   4. Cloud Router
#   5. Subnet
#   6. VPC
#
# Usage:
#   PROJECT_ID=my-project ./gke-teardown.sh
#   PROJECT_ID=my-project ./gke-teardown.sh --yes   # skip confirmation
#
# Overridable env vars match gke-deploy.sh: REGION, ZONE, CLUSTER, VPC, SUBNET,
#                                           ROUTER, NAT, REPO
set -euo pipefail
trap 'echo "ERROR: failed at line $LINENO (exit $?)" >&2' ERR

# ---- Config ---------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:?PROJECT_ID is required, e.g. PROJECT_ID=my-project ./gke-teardown.sh}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER="${CLUSTER:-dev-cluster}"
VPC="${VPC:-dev-vpc}"
SUBNET="${SUBNET:-dev-subnet}"
ROUTER="${ROUTER:-dev-router}"
NAT="${NAT:-dev-nat}"
REPO="${REPO:-api-images}"

ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
skip() { echo "    not found, skipping"; }

command -v gcloud >/dev/null 2>&1 || { echo "gcloud CLI not found" >&2; exit 1; }

# ---- Confirmation -----------------------------------------------------------
cat <<EOF
About to DELETE the following resources in project '$PROJECT_ID':

  GKE cluster          : $CLUSTER (zone $ZONE)
  Artifact Registry    : $REPO (location $REGION) — ALL IMAGES WILL BE DESTROYED
  Cloud NAT / Router   : $NAT / $ROUTER (region $REGION)
  Subnet / VPC         : $SUBNET / $VPC (region $REGION)

EOF
if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Proceed? Type 'delete' to confirm: " CONFIRM
  [[ "$CONFIRM" == "delete" ]] || { echo "Aborted."; exit 0; }
fi

gcloud config set project "$PROJECT_ID"

# ---- 1. GKE cluster ---------------------------------------------------------
log "Deleting cluster: $CLUSTER (this takes several minutes)"
if gcloud container clusters describe "$CLUSTER" --zone="$ZONE" &>/dev/null; then
  gcloud container clusters delete "$CLUSTER" --zone="$ZONE" --quiet
else
  skip
fi

# ---- 2. Artifact Registry repo -----------------------------------------------
log "Deleting Artifact Registry repo: $REPO"
if gcloud artifacts repositories describe "$REPO" --location="$REGION" &>/dev/null; then
  gcloud artifacts repositories delete "$REPO" --location="$REGION" --quiet
else
  skip
fi

# ---- 3. Cloud NAT -------------------------------------------------------------
log "Deleting Cloud NAT: $NAT"
if gcloud compute routers nats describe "$NAT" --router="$ROUTER" --region="$REGION" &>/dev/null; then
  gcloud compute routers nats delete "$NAT" --router="$ROUTER" --region="$REGION" --quiet
else
  skip
fi

# ---- 4. Cloud Router -----------------------------------------------------------
log "Deleting Cloud Router: $ROUTER"
if gcloud compute routers describe "$ROUTER" --region="$REGION" &>/dev/null; then
  gcloud compute routers delete "$ROUTER" --region="$REGION" --quiet
else
  skip
fi

# ---- 5. Subnet -----------------------------------------------------------------
# Retry: NAT IP release can lag briefly behind NAT deletion.
log "Deleting subnet: $SUBNET"
if gcloud compute networks subnets describe "$SUBNET" --region="$REGION" &>/dev/null; then
  for attempt in 1 2 3; do
    if gcloud compute networks subnets delete "$SUBNET" --region="$REGION" --quiet; then
      break
    fi
    if [[ "$attempt" -eq 3 ]]; then
      echo "ERROR: could not delete subnet $SUBNET after 3 attempts." >&2
      echo "Something may still be using it (LB forwarding rules, reserved IPs, other VMs)." >&2
      echo "Check: gcloud compute networks subnets describe $SUBNET --region=$REGION --format='value(ipAddress)'" >&2
      exit 1
    fi
    echo "    retrying in 15s (waiting for NAT IPs to be released)..."
    sleep 15
  done
else
  skip
fi

# ---- 6. VPC ---------------------------------------------------------------------
log "Deleting VPC: $VPC"
if gcloud compute networks describe "$VPC" &>/dev/null; then
  gcloud compute networks delete "$VPC" --quiet
else
  skip
fi

# ---- Summary ----------------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32mTeardown complete.\033[0m') All managed resources deleted; billing for them has stopped.

  Notes:
  - If you reserved any static external IPs yourself, delete them separately:
      gcloud compute addresses list
      gcloud compute addresses delete NAME --region=$REGION
  - Google-managed TLS certificates created by GKE ingress are removed with
    the cluster; cert-manager/Let's Encrypt certs cost nothing and need no cleanup.
  - Cloudflare tunnel/DNS entries are managed in Cloudflare, not GCP — remove
    them there if desired.
EOF
