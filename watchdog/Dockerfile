FROM python:3.12-slim
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY watchdog.py .
# -u: log lines show up in `docker compose logs` at once
CMD ["python", "-u", "watchdog.py"]
