FROM python:3.11-alpine AS builder

RUN apk add --no-cache gcc musl-dev linux-headers

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# crane pushes uploaded `docker save` tarballs to the registry (dashboard).
# Pinned release.
ARG CRANE_VERSION=v0.20.2
RUN wget -q -O /tmp/crane.tgz \
      "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz" \
 && tar -xzf /tmp/crane.tgz -C /usr/local/bin crane \
 && /usr/local/bin/crane version

FROM python:3.11-alpine

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=builder /usr/local/bin/crane /usr/local/bin/crane
COPY . .

USER 1000

EXPOSE 5000

# controller-api. controller-admin overrides the command (admin_api, port 5001).
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "8"]
