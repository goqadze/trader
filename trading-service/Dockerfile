# Small Python base image
FROM python:3.12-slim
WORKDIR /code
# Install dependencies first so Docker caches this layer unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy backend code
COPY app ./app
# ONE worker only: the scheduler runs inside the process, and two workers would trade twice.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
