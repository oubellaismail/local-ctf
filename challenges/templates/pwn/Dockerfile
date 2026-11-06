# Multi-stage build for pwn challenge
# Stage 1: Builder with gcc
FROM ubuntu:22.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY vuln.c .
RUN gcc -fno-stack-protector -z execstack -no-pie -o vuln vuln.c

# Stage 2: Runtime (clean Ubuntu, no gcc)
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends socat && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 1000 appuser && useradd -u 1000 -g 1000 -M appuser

# Copy binary and entrypoint
COPY --from=builder --chown=1000:1000 /build/vuln /usr/local/bin/vuln
COPY --chown=1000:1000 entrypoint.sh /entrypoint.sh

USER 1000
EXPOSE 1337
ENTRYPOINT ["/entrypoint.sh"]
CMD ["socat", "-T60", "TCP-LISTEN:1337,reuseaddr,fork", "EXEC:/usr/local/bin/vuln,nofork"]
