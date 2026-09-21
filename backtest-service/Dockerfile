# Small Python base image
FROM python:3.12-slim
WORKDIR /code
# Install dependencies first so Docker caches this layer unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy backend code
COPY app ./app
# docker-compose overrides this with --reload for development
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
