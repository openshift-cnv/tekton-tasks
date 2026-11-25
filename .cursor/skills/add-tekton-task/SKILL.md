---
name: add-tekton-task
description: Guides adding a new Tekton pipeline task step using the Resilient Execution Layer template. Includes timeout table, validation checklist, and exec_action patterns. Use when adding tasks, creating pipeline steps, or implementing new automation.
---

# Add Tekton Task

## Prerequisites

Before adding a task, determine:

1. **Target pipeline** -- `pipelines/update-bundle.yaml`, `fbc/fbc-post-release.yaml`, or new file?
2. **Position** -- Which task does it run after?
3. **External calls** -- What commands does the step invoke (git, helm, kubectl, curl)?

## Step-by-Step

### 1. Add the task entry to the pipeline

```yaml
- name: my-new-task
  runAfter:
    - <previous-task>
  taskSpec:
    params:
      - name: PARAM_NAME
        description: What this param provides
    results:
      - name: MY_RESULT
        description: What this result contains
    steps:
      - name: run-logic
        image: <image>@sha256:<digest>
        script: |
          #!/usr/bin/env bash
          # Paste the execution layer template below
```

### 2. Apply the execution layer template

Copy from `.cursor/rules/020-execution-layer-reference.mdc`:

```bash
#!/usr/bin/env bash

# ============================================================
# EXECUTION LAYER
# ============================================================
SCRIPT_START=$(date +%s)
declare -a ACTION_LOG=()
FINAL_EXIT_CODE=0

if [[ "$(params.DEBUG)" == "true" ]]; then set -x; fi

# (exec_action and execution_summary functions from template)

trap execution_summary EXIT

# ============================================================
# RUN LOGIC
# ============================================================
set -eo pipefail

# Your business logic here, using exec_action for external calls
```

### 3. Wrap external calls with exec_action

Use the timeout table for appropriate values:

| Command Type | Timeout |
|-------------|---------|
| kubectl/oc API calls | 60s |
| git clone | 120s |
| git pull/push | 60s |
| helm pull/push | 120s |
| helm lint/package | 30s |
| Registry login | 30s |
| Validation checks | 30s |

```bash
if ! exec_action "Clone repository" 120 "git clone \$REPO_URL /workspace/repo"; then
  echo "ERROR: Clone failed"
  FINAL_EXIT_CODE=1
  exit 1
fi
```

### 4. Write results before exit

```bash
echo -n "$VALUE" > $(results.MY_RESULT.path)
FINAL_EXIT_CODE=0
```

### 5. Add debug parameter

Every pipeline MUST have a `debug` param:

```yaml
params:
  - name: debug
    type: string
    default: "false"
    description: Enable debug mode with verbose output (set -x)
```

## Validation After Adding

1. YAML syntax: `python3 -c "import yaml; yaml.safe_load(open('file.yaml'))"`
2. Bash syntax: Extract script, run `bash -n script.sh`
3. Dry-run: `kubectl apply --dry-run=client -f file.yaml`
4. Verify `runAfter` ordering
5. Verify result references use correct task/param names

## Checklist

- [ ] Execution layer header (SCRIPT_START, ACTION_LOG, FINAL_EXIT_CODE)
- [ ] exec_action() for all external calls
- [ ] execution_summary() trap set
- [ ] set -eo pipefail after execution layer
- [ ] All params validated (non-empty, files exist)
- [ ] Results written before every exit path
- [ ] Debug param wired through
- [ ] Image pinned with @sha256: digest
