#!/usr/bin/env bash
# Guard test for the HYDRATED_CHART_NAME validation used by the
# "push-release-chart" step's template-identifier hydration in
# pipelines/update-bundle.yaml (VMER-957).
#
# Extracts validate_hydrated_chart_name() directly out of the pipeline YAML
# (rather than pasting a copy here) so this test can never silently drift
# from the function it's supposed to be guarding.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE_FILE="${REPO_ROOT}/pipelines/update-bundle.yaml"

SCRIPT=$(python3 -c '
import sys, yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["spec"]["tasks"][0]["taskSpec"]["steps"]:
    if step["name"] == "push-release-chart":
        print(step["script"])
        break
else:
    sys.exit("push-release-chart step not found")
' "${PIPELINE_FILE}")

FUNC_DEF=$(printf '%s\n' "${SCRIPT}" | sed -n '/^validate_hydrated_chart_name() {/,/^}/p')

if [[ -z "${FUNC_DEF}" ]]; then
  echo "FAIL: could not extract validate_hydrated_chart_name() from ${PIPELINE_FILE}"
  exit 1
fi

eval "${FUNC_DEF}"

PASS_COUNT=0
FAIL_COUNT=0

# args: description, input, expected (0=pass, 1=fail)
check() {
  local desc="$1" input="$2" expected="$3" actual
  if validate_hydrated_chart_name "$input"; then actual=0; else actual=1; fi
  if [[ "$actual" -eq "$expected" ]]; then
    echo "ok - ${desc}"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo "not ok - ${desc} (input=${input@Q}, expected exit=${expected}, got=${actual})"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

check "empty name is rejected" "" 1
check "shell metacharacter (\$) is rejected" 'hco$(whoami)' 1
check "path separator (/) is rejected" "hco-bundle-registry/rhel10" 1
check "valid hydrated name is accepted" "hco-bundle-registry-rhel10" 0
check "OS-less fallback name is accepted" "hco-bundle-registry" 0

echo "--- ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"
[[ "${FAIL_COUNT}" -eq 0 ]]
