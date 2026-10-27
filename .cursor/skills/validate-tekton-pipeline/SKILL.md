---
name: validate-tekton-pipeline
description: Validates Tekton pipeline and task YAML for syntax errors, bash script correctness, and dry-run apply. Use when editing pipelines, adding tasks, modifying scripts, or before committing.
---

# Validate Tekton Pipeline

## When to Use

- After editing any `pipelines/**/*.yaml` or `fbc/**/*.yaml` file
- After modifying embedded bash scripts in `script:` blocks
- Before committing pipeline changes

## Quick Validation

### 1. YAML Syntax Check

```bash
cd /home/thason/Git/Openshift-virtulization/GitHub/tekton-tasks
python3 -c "
import yaml, sys, pathlib
for f in list(pathlib.Path('pipelines').rglob('*.yaml')) + list(pathlib.Path('fbc').rglob('*.yaml')):
    try:
        list(yaml.safe_load_all(f.read_text()))
    except Exception as e:
        print(f'INVALID: {f}: {e}')
        sys.exit(1)
print('All YAML files valid')
"
```

### 2. Bash Script Syntax Check

Extract the `script:` block from a YAML file and validate bash syntax:

```bash
python3 -c "
import yaml, sys
doc = yaml.safe_load(open(sys.argv[1]))
for task in doc.get('spec', {}).get('tasks', []):
    ts = task.get('taskSpec', {})
    for step in ts.get('steps', []):
        script = step.get('script', '')
        if script:
            fname = f'/tmp/{step[\"name\"]}.sh'
            with open(fname, 'w') as f:
                f.write(script)
            import subprocess
            r = subprocess.run(['bash', '-n', fname], capture_output=True, text=True)
            if r.returncode != 0:
                print(f'SYNTAX ERROR in step {step[\"name\"]}: {r.stderr}')
                sys.exit(1)
            print(f'OK: {step[\"name\"]}')
" pipelines/<name>.yaml
```

### 3. Dry-Run Apply

```bash
kubectl apply --dry-run=client -f pipelines/update-bundle.yaml
kubectl apply --dry-run=client -f fbc/fbc-post-release.yaml
```

## Execution Layer Checks

For steps using the Resilient Execution Layer, verify:

1. `SCRIPT_START`, `ACTION_LOG`, `FINAL_EXIT_CODE` initialized at top
2. `exec_action()` function defined
3. `execution_summary()` function defined
4. `trap execution_summary EXIT` set before business logic
5. `set -eo pipefail` set after execution layer, before business logic

## Helm OCI Chart Lifecycle Checks

For steps that hydrate and push Helm charts (e.g., `push-release-chart`):

### Variable Derivation Chain

Verify the derivation from `BUNDLE_VERSION` is consistent:

```
BUNDLE_VERSION  →  SEMVER (cut -d'-' -f1)
                →  RELEASE_NUM (grep -oE '[0-9]+$')
                →  OS_SUFFIX (cut -d'-' -f2 | cut -d'.' -f1)
                →  XY (cut -d'.' -f1,2 | tr '.' '-')
```

### Chart Name Hydration Contract

1. `HYDRATED_CHART_NAME` must match what `yq` writes to `.name` in Chart.yaml
2. `CHART_VERSION` must match what `yq` writes to `.version` in Chart.yaml
3. `PACKAGE_FILE` must equal `{HYDRATED_CHART_NAME}-{CHART_VERSION}.tgz`
4. `helm package .` produces the .tgz using `.name` and `.version` from Chart.yaml
5. `helm push` reads the chart name from inside the .tgz for the OCI repo path

If any of these are misaligned, the package file check (`! -f "${PACKAGE_FILE}"`) will fail, or the push will target the wrong OCI repo.

### Verification Checklist

- [ ] Chart.yaml `.name` is hydrated (not left as skeleton default)
- [ ] Chart.yaml `.version` is hydrated (not `0.0.0-skeleton`)
- [ ] Chart.yaml `.appVersion` is hydrated
- [ ] Verification reads back `.name` and `.version` and compares
- [ ] `PACKAGE_FILE` uses `HYDRATED_CHART_NAME`, not `CHART_NAME`
- [ ] Published message uses `HYDRATED_CHART_NAME`
- [ ] OS suffix guard handles: normal (`rhel9`), future (`rhel10`), null, empty, legacy (no dash)

### Cross-Step Result Checks

- [ ] Step 1 writes `bundle-version` and `snapshots-to-release` to `/tekton/results/`
- [ ] Step 2 validates both files exist before reading
- [ ] Step 2 validates `bundle-version` is non-empty
- [ ] `snapshots-to-release` JSON is validated with `jq -e .` before injection

## Pre-Commit Workflow

1. Edit the pipeline/task YAML
2. Run YAML syntax check
3. Run bash syntax check on modified scripts
4. Run `kubectl apply --dry-run=client`
5. Verify chart hydration contract (if modifying push-release-chart step)
6. Commit with format: `<type>(<scope>): <description>`
