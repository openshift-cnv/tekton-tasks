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

## Pre-Commit Workflow

1. Edit the pipeline/task YAML
2. Run YAML syntax check
3. Run bash syntax check on modified scripts
4. Run `kubectl apply --dry-run=client`
5. Commit with format: `<type>(<scope>): <description>`
