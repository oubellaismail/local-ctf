FROM python:3.11-alpine
RUN apk add --no-cache socat
WORKDIR /app
COPY challenge.py .
RUN adduser -D -u 1000 ctf
USER 1000
EXPOSE 4444
CMD ["socat", "TCP-LISTEN:4444,reuseaddr,fork", "EXEC:python /app/challenge.py,pty,stderr"]
