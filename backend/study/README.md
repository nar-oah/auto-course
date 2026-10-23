# Temporary study diagnostics

Set `STUDY_TRACE=1` in the **study worker's environment**, then restart the worker.
Any other value, including an unset variable, disables collection and output.
Diagnostics use the existing `auto_course_study.trace` logger (INFO for normal
completion, WARNING when anomalies occur); the worker already runs at INFO.
There are no additional requests, files, dependencies or database writes.

Each domain task emits one JSON record when it exits, including its Celery task
ID, domain, UTC timestamps, monotonic durations and final study result. A Celery
return of `returned` can accompany a study result such as `login_failed`,
`progress_rejected`, `progress_unconfirmed` or `failure_budget_exhausted`.
Successful traversal is `completed`; an empty enumeration is `empty_video_list`.

The collector keeps the latest 256 events. If an anomaly occurs, even when it is
recovered, the final record also includes the buffered events. Task statistics,
the first anomaly and the last progress/HTTP study/heartbeat results survive
event eviction. Request concentration is counted in at most 32 one-minute
windows. Course and pending-video counts reflect lazy enumeration;
`catalog_complete=false` means those counts may be partial. Video and study
identifiers are anonymous and consistent only within that task.

Remove the environment variable and restart the worker to disable diagnostics.
All diagnostic tests use `httpx.MockTransport` and mocked waits. From `backend`:

```sh
uv run --all-packages pytest study/tests
```
