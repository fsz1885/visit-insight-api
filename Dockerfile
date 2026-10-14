FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY visit_workflow/ ./visit_workflow/
COPY examples/ ./examples/
RUN mkdir -p runs && python -m visit_workflow.demo
EXPOSE 8767
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8767/healthz', timeout=3)"
CMD ["python", "-m", "visit_workflow", "serve", "--host", "0.0.0.0", "--port", "8767", "--no-browser"]
