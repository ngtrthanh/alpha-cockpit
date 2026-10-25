FROM python:3.15.1-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()" || exit 1
CMD ["python", "app.py"]
