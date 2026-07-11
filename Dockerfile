FROM python:3.11-slim

# Install Node.js, Nginx, gettext
RUN apt-get update && apt-get install -y \
    curl \
    nginx \
    gettext-base \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Install Node.js dependencies for vibe-coding
RUN cd vibe-coding && npm install --production

# Make start.sh executable
RUN chmod +x start.sh

EXPOSE $PORT

CMD ["bash", "start.sh"]
