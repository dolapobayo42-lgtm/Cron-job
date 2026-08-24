FROM python:3.11-slim

# System deps: Tesseract OCR binary + Chromium runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + its OS-level dependencies for Playwright
RUN playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py"]
