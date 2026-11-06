FROM ubuntu:22.04
RUN apt-get update && apt-get install -y socat build-essential --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY check.c .
RUN gcc -o check check.c && chmod +x check
RUN useradd -m -u 1000 ctf
USER 1000
EXPOSE 1337
CMD ["socat", "TCP-LISTEN:1337,reuseaddr,fork", "EXEC:./check"]
