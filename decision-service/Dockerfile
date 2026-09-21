# Small Python base image
FROM python:3.12-slim
# All following commands run from /code inside the container
WORKDIR /code
# Copy requirements first so Docker caches the (slow) pip install unless dependencies change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy the application code last (it changes most often)
COPY app ./app
# Default start command (docker-compose overrides it with --reload for development)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
