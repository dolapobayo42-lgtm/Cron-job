FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Tesseract OCR binary (not included in the Playwright base image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browsers are already present in this base image, but this ensures
# the exact chromium build matching playwright==1.47.0 is installed.
RUN playwright install chromium

COPY . .

CMD ["python", "main.py"]
