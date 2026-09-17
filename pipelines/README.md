# CNV Tekton Pipelines

Tekton pipelines for CNV component builds and releases in Konflux.

## Pipelines

### update-bundle

Updates the HCO bundle registry with component snapshots and publishes the release orchestrator chart.

**Trigger:** Runs when a component snapshot is created for `hco-bundle-registry`.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `snapshot` | string | Yes | — | Snapshot reference (e.g., `namespace/snapshot-id`) |
| `skeleton-chart-version` | string | No | `0.0.0-skeleton` | Version of skeleton chart to pull from OCI registry |

**Steps:**

1. **update-bundle**: Updates `hco-bundle-registry` GitLab repo with component image references
2. **push-release-chart**: Pulls skeleton chart, hydrates with snapshots, and pushes versioned chart

**Secrets Required:**

| Secret | Keys | Purpose |
|--------|------|---------|
| `hco-updater-gitlab-token` | `GITLAB_TOKEN` | GitLab repo access |
| `quay-builds-creds` | `.dockerconfigjson` | OCI registry push |

**Outputs:**

- `bundle-version`: Version string (e.g., `4.99.0-rhel9.2555`)
- `snapshots-to-release`: JSON map of component → snapshot ID

**Version Format:**

```
Input:  build-bundle.json { XY: "v4.99", Z: "0", os: "rhel9", release: "2555" }
Output: 4.99.0-rhel9.2555
```

**Dual ReleasePlan:**

The pipeline sets two ReleasePlans in the chart:
- `releasePlan`: For regular components (kubevirt, cdi, etc.)
- `bundleReleasePlan`: For `hco-bundle-registry` (separate Konflux pipeline)

---

## Development

### Testing Locally

The pipelines require Konflux cluster access. For local validation:

```bash
# Validate YAML syntax
kubectl apply --dry-run=client -f pipelines/update-bundle.yaml
```

### Required Tools in Pipeline Images

- `kubectl` - Kubernetes API access
- `jq` - JSON processing
- `yq` - YAML processing
- `helm` (v3.x) - Chart operations
- `git` - Repository operations
