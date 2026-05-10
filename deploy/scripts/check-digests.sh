#!/usr/bin/env bash
# Verify every FROM / COPY --from line in every Dockerfile pins an
# image by digest (@sha256:…).  Floating tags are rejected so a
# compromised upstream image or a retagged :latest cannot sneak into
# the build.
#
# Usage:
#   deploy/scripts/check-digests.sh
#
# Exit 0 when all images are pinned; exit 1 with a message otherwise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FAILED=0

while IFS= read -r dockerfile; do
    [[ -z "$dockerfile" ]] && continue
    while IFS= read -r line; do
        # Skip comment lines and blank lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        # Extract the image reference: FROM <image> or COPY --from=<image>
        if [[ "$line" =~ ^FROM[[:space:]]+([^[:space:]]+) ]]; then
            image="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ COPY[[:space:]]+--from=([^[:space:]]+) ]]; then
            image="${BASH_REMATCH[1]}"
        else
            continue
        fi

        # Skip scratch and build-stage references
        [[ "$image" == "scratch" ]] && continue
        [[ "$image" =~ ^builder$ ]] && continue

        if [[ ! "$image" =~ @sha256:[a-f0-9]{64}$ ]]; then
            echo "ERROR: unpinned image in $dockerfile — $image"
            echo "       Pin it with @sha256:… (use deploy/scripts/refresh-digests.sh)"
            FAILED=1
        fi
    done < "$dockerfile"
done < <(find "$REPO_ROOT" -name 'Dockerfile' -o -name 'Dockerfile.*' | sort)

if [[ $FAILED -eq 1 ]]; then
    echo ""
    echo "One or more Dockerfiles contain floating image tags."
    echo "All FROM and COPY --from lines must include an @sha256: digest."
    exit 1
fi

echo "All Dockerfile images are pinned by digest."
