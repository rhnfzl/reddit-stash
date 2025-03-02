# Use a slim Python base image for smaller size
FROM python:3.10-slim

# Set environment variables to prevent Python from writing bytecode and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create a non-root user for security
RUN useradd -m -s /bin/bash appuser

# Set the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Create necessary directories and set permissions
RUN mkdir -p reddit && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Set environment variables (these can be overridden at runtime)
ENV REDDIT_CLIENT_ID=None \
    REDDIT_CLIENT_SECRET=None \
    REDDIT_USERNAME=None \
    REDDIT_PASSWORD=None \
    DROPBOX_APP_KEY=None \
    DROPBOX_APP_SECRET=None \
    DROPBOX_REFRESH_TOKEN=None

# Create a volume mount point for persisting data
VOLUME ["/app/reddit"]

# Provide options to run different scripts
ENTRYPOINT ["python"]
CMD ["reddit_stash.py"]

# Usage instructions as comments
# To build: docker build -t reddit-stash .
#
# To run with environment variables:
# docker run -it \
#   -e REDDIT_CLIENT_ID=your_client_id \
#   -e REDDIT_CLIENT_SECRET=your_client_secret \
#   -e REDDIT_USERNAME=your_username \
#   -e REDDIT_PASSWORD=your_password \
#   -e DROPBOX_APP_KEY=your_dropbox_key \
#   -e DROPBOX_APP_SECRET=your_dropbox_secret \
#   -e DROPBOX_REFRESH_TOKEN=your_dropbox_token \
#   -v /path/on/host/reddit:/app/reddit \
#   reddit-stash
#
# To run dropbox upload:
# docker run -it \
#   -e DROPBOX_APP_KEY=your_dropbox_key \
#   -e DROPBOX_APP_SECRET=your_dropbox_secret \
#   -e DROPBOX_REFRESH_TOKEN=your_dropbox_token \
#   -v /path/on/host/reddit:/app/reddit \
#   reddit-stash dropbox_utils.py --upload
#
# To run dropbox download:
# docker run -it \
#   -e DROPBOX_APP_KEY=your_dropbox_key \
#   -e DROPBOX_APP_SECRET=your_dropbox_secret \
#   -e DROPBOX_REFRESH_TOKEN=your_dropbox_token \
#   -v /path/on/host/reddit:/app/reddit \
#   reddit-stash dropbox_utils.py --download