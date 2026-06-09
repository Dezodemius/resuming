FROM python:3.12-slim

# Install Ollama
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://ollama.com/install.sh | sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000

# Стартовый скрипт: запускаем ollama в фоне, пулим модель, стартуем FastAPI
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
