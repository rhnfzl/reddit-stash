# Use a slim Python base image for smaller size
FROM python:3.10-slim

# Create a non-root user for security
RUN useradd -m -s /bin/bash appuser

# Set the working directory
WORKDIR /app

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

# Set environment variables (these will need to be provided at runtime)
ENV REDDIT_CLIENT_ID=None \
    REDDIT_CLIENT_SECRET=None \
    REDDIT_USERNAME=None \
    REDDIT_PASSWORD=None \
    DROPBOX_APP_KEY=None \
    DROPBOX_APP_SECRET=None \
    DROPBOX_REFRESH_TOKEN=None

# Run the script
CMD ["python", "reddit_stash.py"]