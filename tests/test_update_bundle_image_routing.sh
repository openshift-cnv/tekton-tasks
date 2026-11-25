#!/usr/bin/env bash
# Guard test for image routing (core vs extra params) and test application
# filtering in pipelines/update-bundle.yaml (VMER-2503).
#
# Extracts update_images_snapshots() and snapshot-saving logic directly out of
# the pipeline YAML so this test can never silently drift.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE_FILE="${REPO_ROOT}/pipelines/update-bundle.yaml"

SCRIPT=$(python3 -c '
import sys, yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["spec"]["tasks"][0]["taskSpec"]["steps"]:
    if step["name"] == "update-bundle":
        print(step["script"])
        break
else:
    sys.exit("update-bundle step not found")
' "${PIPELINE_FILE}")

# Extract update_images_snapshots() function definition
FUNC_DEF=$(printf '%s\n' "${SCRIPT}" | sed -n '/^function update_images_snapshots() {/,/^}/p')

if [[ -z "${FUNC_DEF}" ]]; then
  echo "FAIL: could not extract update_images_snapshots() from ${PIPELINE_FILE}"
  exit 1
fi

# Extract the condition at line 340 (Save Snapshot object for APPLICATION)
# Grep for the if condition checking APPLICATION before snapshot_objects_dir
SAVE_SNAP_IF_COND=$(python3 -c '
import sys
script = sys.stdin.read()
import re
# Find the if condition right before snapshot_objects_dir="components/hco-bundle-registry/snapshot-objects"
match = re.search(r"if \[\[.*?\$APPLICATION.*?\]\]; then\s+snapshot_objects_dir=", script)
if match:
    # return just the condition part e.g. [[ ... ]]
    cond = re.search(r"\[\[.*?\]\]", match.group(0))
    print(cond.group(0))
else:
    sys.exit("condition not found")
' <<< "${SCRIPT}")

if [[ -z "${SAVE_SNAP_IF_COND}" ]]; then
  echo "FAIL: could not extract snapshot object saving condition from ${PIPELINE_FILE}"
  exit 1
fi

PASS_COUNT=0
FAIL_COUNT=0

# Create a temporary workspace for executing update_images_snapshots
TMP_DIR=$(mktemp -d)
trap 'rm -rf "${TMP_DIR}"' EXIT

mkdir -p "${TMP_DIR}/components/hco-bundle-registry"

# Helper function to classify image routing using extracted update_images_snapshots
# returns "core" or "extra"
classify_image() {
  local image_name="$1"
  local core_file="${TMP_DIR}/components/hco-bundle-registry/core-params.json"
  local extra_file="${TMP_DIR}/components/hco-bundle-registry/extra-params.json"
  echo "{}" > "$core_file"
  echo "{}" > "$extra_file"

  (
    cd "${TMP_DIR}"
    snapshots_file="components/hco-bundle-registry/snapshots.json"
    eval "${FUNC_DEF}"
    IMAGES="registry.example.com/org/${image_name}@sha256:1234567890abcdef"
    APPLICATION="dummy"
    SNAPSHOT_ID="dummy-snap"
    update_images_snapshots > /dev/null 2>&1
  )

  if jq -e ".\"${image_name}-image\"" "$core_file" > /dev/null 2>&1; then
    echo "core"
  elif jq -e ".\"${image_name}-image\"" "$extra_file" > /dev/null 2>&1; then
    echo "extra"
  else
    echo "unknown"
  fi
}

# Helper function to classify application in update_images_snapshots (line 301)
# returns "included" or "skipped"
classify_app_snapshots() {
  local app_name="$1"
  local snap_file="${TMP_DIR}/components/hco-bundle-registry/snapshots.json"
  echo "{}" > "$snap_file"

  (
    cd "${TMP_DIR}"
    snapshots_file="components/hco-bundle-registry/snapshots.json"
    eval "${FUNC_DEF}"
    IMAGES=""
    APPLICATION="$app_name"
    SNAPSHOT_ID="snap-test-id"
    update_images_snapshots > /dev/null 2>&1
  )

  if jq -e ".\"${app_name}\"" "$snap_file" > /dev/null 2>&1; then
    echo "included"
  else
    echo "skipped"
  fi
}

# Helper function to classify application in save snapshot object logic (line 340)
# returns "included" or "skipped"
classify_app_save_snapshot() {
  local APPLICATION="$1"
  if eval "${SAVE_SNAP_IF_COND}"; then
    echo "included"
  else
    echo "skipped"
  fi
}

check_image_routing() {
  local image_name="$1"
  local expected="$2"
  local actual
  actual=$(classify_image "$image_name")
  if [[ "$actual" == "$expected" ]]; then
    echo "ok - image routing: ${image_name} -> ${expected}"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "not ok - image routing: ${image_name} -> expected ${expected}, got ${actual}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

check_app_snapshots() {
  local app_name="$1"
  local expected="$2"
  local actual
  actual=$(classify_app_snapshots "$app_name")
  if [[ "$actual" == "$expected" ]]; then
    echo "ok - snapshots.json app filter (line 301): ${app_name} -> ${expected}"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "not ok - snapshots.json app filter (line 301): ${app_name} -> expected ${expected}, got ${actual}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

check_app_save_snapshot() {
  local app_name="$1"
  local expected="$2"
  local actual
  actual=$(classify_app_save_snapshot "$app_name")
  if [[ "$actual" == "$expected" ]]; then
    echo "ok - snapshot object app filter (line 340): ${app_name} -> ${expected}"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "not ok - snapshot object app filter (line 340): ${app_name} -> expected ${expected}, got ${actual}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

echo "# Testing image routing (core vs extra params)..."
check_image_routing "kubevirt-operator-rhel9" "core"
check_image_routing "virt-operator" "core"
check_image_routing "virt-platform-autopilot" "core"
check_image_routing "virt-platform-autopilot-rhel9" "core"
check_image_routing "vm-file-restore-operator-rhel9" "core"
check_image_routing "vm-file-restore-operator-test" "extra"
check_image_routing "vm-file-restore-operator-test-rhel9" "extra"
check_image_routing "hyperconverged-cluster-operator-test-rhel10" "extra"
check_image_routing "cdi-test-utils" "extra"
check_image_routing "quay-prefix" "extra"

echo "# Testing APPLICATION classification for snapshots.json (line 301)..."
check_app_snapshots "vm-file-restore-operator-test" "skipped"
check_app_snapshots "kubevirt-operator-test-rhel9" "skipped"
check_app_snapshots "vm-file-restore-operator" "included"
check_app_snapshots "hyperconverged-cluster-operator" "included"

echo "# Testing APPLICATION classification for snapshot objects (line 340)..."
check_app_save_snapshot "vm-file-restore-operator-test" "skipped"
check_app_save_snapshot "kubevirt-operator-test-rhel9" "skipped"
check_app_save_snapshot "vm-file-restore-operator" "included"
check_app_save_snapshot "hyperconverged-cluster-operator" "included"

echo "--- ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"
[[ "${FAIL_COUNT}" -eq 0 ]]
