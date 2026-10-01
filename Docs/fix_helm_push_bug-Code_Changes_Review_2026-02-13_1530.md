# Code Changes Review: Fix Helm Chart Push Bug

**Plan:** `fix_helm_push_bug_d166770e.plan.md`
**Date:** 2026-02-13
**File:** `pipelines/update-bundle.yaml`
**Diff:** 4 hunks, +15 / -2 lines (net +13)

---

## 1. Developer + Technical Impact Summary

* **Risk Level:** Low
* **Breaking Changes:** None. The pipeline's external interface is unchanged:
  * **Params:** `snapshot`, `skeleton-chart-version`, `debug` -- no change
  * **Results:** `snapshots-to-release`, `bundle-version` -- no change
  * **Step contract:** Shared `/tekton/results/` volume between steps -- no change
  * **Secrets:** `hco-updater-gitlab-token`, `quay-builds-creds` -- no change
* **Behavioral Change (intended):** The OCI push now uses the correct versioned `.tgz` file instead of the stale skeleton archive. Downstream Kargo Warehouses will see correct version tags (e.g., `4.21.1-releases.23`) instead of `0.0.0-skeleton`.

---

## 2. Downstream Impact Analysis

| Consumer | Location | Impact | Risk |
| :--- | :--- | :--- | :--- |
| `fbc-post-release.yaml` | `fbc/fbc-post-release.yaml` | None -- independent pipeline, does not consume `update-bundle` results | None |
| Kargo Warehouses (OCI watch) | External (Konflux cluster) | **Positive** -- will now detect correct version tags from Quay OCI registry | None (fix restores intended behavior) |
| ArgoCD Sync | External (Konflux cluster) | **Positive** -- promotions triggered by Kargo will use the hydrated chart with correct values | None |
| `pipelines/README.md` | Same repo | No update needed -- documents params/results which are unchanged | None |
| Execution summary output | Pipeline logs | Two new ACTION_LOG entries appear: `Cleanup stale tgz archives`, `Verify Chart.yaml hydration`. Does not affect parsing -- log consumers read exit codes, not action names. | None |

**Existing tests:** No automated test suite exists for this pipeline (Tekton pipelines are validated via `kubectl apply --dry-run=client` per README). No risk of test failure.

---

## 3. Findings and Fixes

| File | Line | Severity | Issue Type | Description and Fix |
| :--- | :--- | :--- | :--- | :--- |
| `update-bundle.yaml` | 651 | LOW | Robustness | The `yq` read-back runs under `set -eo pipefail`. If `yq` fails (unlikely since it just wrote to the same file), `set -e` aborts the script but `FINAL_EXIT_CODE` remains `0`, causing the execution summary trap to exit `0`. **This is a pre-existing pattern issue**, not introduced by this change. To harden, wrap with explicit error handling: `ACTUAL_VERSION=$(yq '.version' Chart.yaml \| tr -d '"') \|\| { echo "ERROR: ..."; FINAL_EXIT_CODE=1; exit 1; }`. **Verdict: acceptable as-is** -- the preceding `exec_action` just used `yq -i` on the same file, so a read failure here would indicate a catastrophic environment problem. |
| `update-bundle.yaml` | 708 | LOW | Dead code | The `-z "${PACKAGE_FILE}"` check is now redundant since the variable is constructed from two previously-validated non-empty strings (`CHART_NAME` at line 530 is a constant, `CHART_VERSION` at line 638 is validated at lines 632-636). **Verdict: keep it** -- it costs nothing and provides defense-in-depth if the variable construction logic is ever refactored. |
| `update-bundle.yaml` | 616 | INFO | Style | The `ACTION_LOG` entry for stale `.tgz` cleanup always reports `OK` regardless of whether files were actually removed. This is acceptable since `rm -f` is idempotent and the action's purpose is cleanup, not validation. |

**No HIGH or CRITICAL issues found.**

---

## 4. Verification Plan

### Pre-merge (local)

1. **YAML syntax validation:**

```bash
python3 -c "import yaml; yaml.safe_load(open('pipelines/update-bundle.yaml'))"
```

2. **Bash syntax validation** -- extract the `push-release-chart` script and check:

```bash
# Extract script block and validate syntax
yq '.spec.tasks[0].taskSpec.steps[1].script' pipelines/update-bundle.yaml > /tmp/push-release-chart.sh
bash -n /tmp/push-release-chart.sh
```

3. **Dry-run apply:**

```bash
kubectl apply --dry-run=client -f pipelines/update-bundle.yaml
```

### Post-merge (pipeline run)

4. **Trigger a test run** against a non-production `hco-bundle-registry` snapshot. In the pipeline log, verify the five-line consistency check:

```
ACTION: Package chart   --> ...hco-bundle-registry-rhel9-X.Y.Z-releases.N.tgz
OK: Packaged as         --> ...hco-bundle-registry-rhel9-X.Y.Z-releases.N.tgz
CMD: helm push          --> ...hco-bundle-registry-rhel9-X.Y.Z-releases.N.tgz
Pushed:                 --> ...hco-bundle-registry-rhel9:X.Y.Z-releases.N
Published               --> ...hco-bundle-registry-rhel9:X.Y.Z-releases.N (file: ...)
```

All five lines MUST show the same version string. The previously broken run showed `0.0.0-skeleton` on lines 2-5 while line 1 had the correct version.

5. **Verify Quay OCI tag** -- after the pipeline completes, confirm the chart exists at the expected tag:

```bash
helm show chart oci://quay.io/openshift-virtualization/konflux-builds/v4-21/hco-bundle-registry-rhel9 --version 4.21.1-releases.23
```

6. **Verify new ACTION_LOG entries** in the execution summary:

```
Cleanup stale tgz archives
Verify Chart.yaml hydration
```

Both should appear as `OK` in the summary.

### Regression check

7. **Non-hco-bundle-registry snapshot:** Run the pipeline with a non-bundle snapshot (e.g., a `kubevirt` component). Verify the `push-release-chart` step correctly skips at Phase 1 with `SKIP: Snapshot does not contain 'hco-bundle-registry'`. This flow is not touched by the changes but confirms no collateral damage.
