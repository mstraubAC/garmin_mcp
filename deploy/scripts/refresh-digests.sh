#!/usr/bin/env bash
# Refresh pinned image digests in deploy/Dockerfile*.
#
# Usage:
#   deploy/scripts/refresh-digests.sh
#
# Pulls each base image, reads its digest, and updates the FROM line.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/.."

images=(
    "ghcr.io/astral-sh/uv:0.5-python3.13-bookworm-slim"
    "python:3.13-slim"
    "caddy:2-builder"
    "caddy:2-alpine"
)

for image in "${images[@]}"; do
    echo "Pulling $image..."
    docker pull "$image" >&2
    digest=$(docker image inspect "$image" --format='{{index .RepoDigests 0}}' | cut -d@ -f2)
    echo "  $image@sha256:$digest"
done

echo ""
echo "Update the FROM lines in $DEPLOY_DIR/Dockerfile and $DEPLOY_DIR/Dockerfile.caddy"
echo "with the digests above. Format: FROM <image>@sha256:<digest>"
