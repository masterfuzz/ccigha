# Usage

```
python convert.py [-s SSH_KEY_SECRET_NAME] cci_workflow_path github_directory [job1 job2 job3...]
```

Example
```
python convert.py -s MY_CI_SSH_GITHUB_SECRET workflow.yaml ~/some/repo/.github build lint
```
