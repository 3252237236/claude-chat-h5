FROM python:3.11-slim

WORKDIR /app

COPY . /app

EXPOSE 8765

CMD ["python", "server.py"]
