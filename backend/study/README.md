# Study

学习业务来自旧仓库 `auto_course-server` 的 `auto.py`、`list.py`、
`response.py`、`urls.py`。保留原版 requests 会话、验证码、登录、课程和
视频遍历、进度提交、心跳、重试及异常处理；仅移除独立调试入口和测试账号。
不包含旧版 FastAPI 服务、日志文件或日志目录管理。

`main.py` 仅负责 Celery 适配，消息契约沿用 `auto-bak`：

- `study.start`：`args=[{"username": "...", "password": "..."}]`。
- `study.run_domain`：`args=[domain, {"username": "...", "password": "..."}]`。
- 两类任务均使用 `study` 队列、JSON 消息、忽略结果，不配置结果后端。
- 每个平台单独派发任务，分别创建原版 `CourseBot` 和 requests 会话。
- Redis 使用 `CELERY_BROKER_URL`，默认 `redis://redis:6379/0`。

在 `backend/study` 目录启动（与当前 Dockerfile 一致）：

```sh
celery -A main:celery_app worker -Q study --loglevel=info
```

已安装依赖后，在 `backend` 目录运行离线回归测试：

```sh
uv run --no-sync --package study python -m unittest discover -s study/tests -v
```

测试拦截 HTTP，模拟 OCR、等待和心跳线程，不访问真实学习平台。
