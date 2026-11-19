# 演示镜像：应用零依赖（标准库），构建时生成演示库，容器起来即可问数。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

COPY . .
# 构建时先 seed 一份；如果运行时把空 volume 挂到 /app/data，entrypoint 会补一次
RUN chmod +x scripts/docker-entrypoint.sh \
    && python scripts/seed_demo_db.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["python", "main.py"]
