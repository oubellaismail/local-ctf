# Multi-stage build for web challenge
# Stage 1: Builder with Python package dependencies
FROM python:3.11-alpine AS builder
WORKDIR /install
COPY requirements.txt .
RUN pip install --prefix=/install/deps --no-cache-dir -r requirements.txt

# Stage 2: Runtime (clean, no build tools)
FROM python:3.11-alpine

# Copy installed packages from builder
COPY --from=builder /install/deps /usr/local

# Create non-root user
RUN adduser -D -u 1000 appuser

# Create /tmp with sticky bit (required for read-only FS at runtime)
RUN mkdir -p /tmp && chmod 1777 /tmp

# Copy app files owned by uid 1000
WORKDIR /app
COPY --chown=1000:1000 entrypoint.sh /entrypoint.sh
COPY --chown=1000:1000 app.py .

USER 1000
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "app.py"]
