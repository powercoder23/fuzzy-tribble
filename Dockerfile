FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y git


COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create directories with proper permissions
RUN mkdir -p /app/data/tokens /app/data/signals /app/logs && \
    chmod -R 777 /app/data /app/logs

COPY . /app

# CMD ["python", "main.py"]
