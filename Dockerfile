FROM python:3.11-slim

# Install ffmpeg (needed for audio conversion)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Railway sets PORT automatically
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 60 --workers 2 app:app
