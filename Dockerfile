# Build:  docker build -t parity .     Run:  docker run -p 8000:8000 --env-file .env parity
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends gnucobol default-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd -m parity
COPY backend/ backend/
COPY frontend/ frontend/
RUN mkdir -p results && chown parity results
# Keep the supervisor root so it can setuid/setgid compiler and Java children to nobody (65534).
ENV PARITY_BUDGET_USD=20 PARITY_MAX_JOBS=2
ENV PORT=8000
EXPOSE 8000
CMD ["python", "backend/server.py"]
