# Use the official Microsoft Playwright image (contains OS browser dependencies)
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

# Install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your backend code
COPY . .

# Render exposes port 10000 by default
EXPOSE 10000

# Start the FastAPI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
