# API

HTTP 服务迁移自 `auto-bak/backend/api`，保留 `auto_course_api` 包结构。
`POST /start` 校验账号密码，向 `study` 队列发送 `study.start`，成功后
返回空的 `202 Accepted` 响应。消息参数为
`args=[{"username": "...", "password": "..."}]`，与当前 study worker 兼容。
不保存任务结果，也不提供任务状态或日志查询接口。

在 `backend` 目录安装依赖并启动：

```sh
uv sync --package api
CELERY_BROKER_URL=redis://127.0.0.1:6379/0 uv run --package api uvicorn auto_course_api.main:app --host 127.0.0.1 --port 8000
```

API 与 study 必须使用相同的 `CELERY_BROKER_URL`，默认值为
`redis://redis:6379/0`。`CORS_ALLOW_ORIGINS` 接受逗号分隔的前端来源，
默认值为 `*`。生产代理剥离 `/auto-course` 前缀，FastAPI 的 `root_path`
保留此前缀供 OpenAPI 文档使用。

在 `backend` 目录运行回归测试：

```sh
uv run --all-packages pytest
```

测试使用 mock 和内存消息队列，不连接真实 Redis 或学习平台。
