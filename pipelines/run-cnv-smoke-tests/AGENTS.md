# Agent Documentation for `run-cnv-smoke-tests`

This directory contains the Tekton pipeline definition and the `gangway.py` script used to bridge Tekton pipelines with OpenShift CI (Prow).

## Setup & Execution
- Dependency manager: `uv`. Use `uv sync` to install.

## Coding Standards
- All public functions must have docstrings and type hints.
- Use `Ruff` for linting and formatting.

## System Architecture

1.  **Tekton Pipeline (`pipeline.yaml`)**:
    -   Orchestrates the smoke test workflow.
    -   Accepts git coordinates (`git-url`, `revision`) pointing to a payload configuration.
    -   Extracts metadata (versions, images) from `payload.yaml` in the target repo.
    -   Delegates the actual test execution to a Prow Job using `gangway.py`.
    -   Records the Prow Job status back to the git repository in `smokeResults.yaml`.

2.  **Gangway Client (`gangway.py`)**:
    -   A Python CLI wrapper for the OpenShift CI Gangway API.
    -   Located in the container image `quay.io/cnv-qe-devops/gangway` (built from this directory).
    -   Handles authentication, job triggering, polling, and artifact retrieval.

## `pipeline.yaml` Details

-   **Task**: `run-smoke-tests`
-   **Step**: `trigger-prow-job-via-gangway`
    -   **Environment**:
        -   `GANGWAY_TOKEN`: Secret for API access.
        -   `PROW_JOB_NAME`: The target job to run (passed via params).
    -   **Logic**:
        1.  Clone target repo.
        2.  Read `payload.yaml` using `yq`.
        3.  Trigger job: `uv run gangway.py --trigger ...`
        4.  Wait for job: `uv run gangway.py --wait-job-id ...`
        5.  Generate `smokeResults.yaml` with job URL and state.
        6.  Commit and push changes.

## `gangway.py` Details

-   **Path**: `pipelines/run-cnv-smoke-tests/gangway.py`
-   **Execution**: Designed to run via `uv` or directly with Python.
-   **Key Arguments**:
    -   `--trigger <JOB_NAME>`: Starts a new job.
    -   `-e KEY=VAL`: Sets environment variables for the job.
    -   `--write-job-info-file <FILE>`: Dumps JSON/YAML job details (ID, URL) for downstream consumption.
    -   `--wait-job-id <ID>`: Polls until the job reaches a terminal state.
-   **Dependencies**: `requests`, `PyYAML` (implied).

## Operational Notes for Agents

-   **Context**: This pipeline acts as a "remote controller" for Prow jobs. It doesn't run tests itself; it triggers them elsewhere.
-   **Debugging**: If `gangway.py` fails, check `GANGWAY_TOKEN` validity and Prow/Gangway URL reachability.
-   **Outputs**: The ultimate output is a git commit to the source repo with `smokeResults.yaml`.
