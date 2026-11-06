# Multi-stage build for misc challenge
# Stage 1: Builder (reserve for future pip dependencies)
FROM python:3.11-alpine AS builder
# No pip deps for misc currently — stage reserved for future extensibility

# Stage 2: Runtime
FROM python:3.11-alpine

# Install socat (system package)
RUN apk add --no-cache socat

# Create non-root user
RUN adduser -D -u 1000 appuser

# Create /tmp with sticky bit (required for read-only FS at runtime)
RUN mkdir -p /tmp && chmod 1777 /tmp

# Copy files
WORKDIR /app
COPY --chown=1000:1000 entrypoint.sh /entrypoint.sh
COPY --chown=1000:1000 puzzle.py .

USER 1000
EXPOSE 4444
ENTRYPOINT ["/entrypoint.sh"]
CMD ["socat", "-T60", "TCP-LISTEN:4444,reuseaddr,fork", "EXEC:python3 /app/puzzle.py,nofork"]
