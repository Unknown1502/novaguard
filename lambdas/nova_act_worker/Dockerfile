FROM python:3.12-slim

# Install system dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser (Chromium) in a world-accessible path
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium --with-deps \
    && chmod -R a+rx /ms-playwright

COPY main.py .

# Run as non-root for security
RUN useradd -m -u 1000 novaact
USER novaact

ENTRYPOINT ["python", "main.py"]
