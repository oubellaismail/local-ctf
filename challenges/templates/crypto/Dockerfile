FROM python:3.11-alpine
RUN pip install flask --no-cache-dir
WORKDIR /app
COPY app.py .
RUN adduser -D -u 1000 ctf
USER 1000
EXPOSE 8080
CMD ["python", "app.py"]
