FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright setup (if user wants JS rendering)
RUN pip install playwright
RUN playwright install chromium
RUN playwright install-deps

# Copy app
COPY . .

# Expose port
EXPOSE 5002

# Run the app
CMD ["python", "app.py"]
