# Render: long SSE stream /progress/*/stream must not timeout the sync worker
# Default gunicorn timeout 30s kills the worker mid-batch (seen WORKER TIMEOUT
# pid 67 -> SIGKILL -> login loop due to stream holding the single sync worker).
timeout = 120
workers = 2
worker_class = "gthread"
threads = 4
graceful_timeout = 30
keepalive = 5
