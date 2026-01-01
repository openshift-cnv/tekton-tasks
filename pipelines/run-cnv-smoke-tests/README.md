## Build the image

```sh
podman build -t quay.io/cnv-qe-devops/gangway -f Dockerfile .
```

## PipelineRun Requirements

This pipeline requires the following secrets to be available in the namespace:

- GIT_USER_NAME
- GIT_USER_EMAIL
- GITHUB_APP_ID
- GITHUB_TOKEN
- GITHUB_APP_PRIVATE_KEY
- GANGWAY_TOKEN
