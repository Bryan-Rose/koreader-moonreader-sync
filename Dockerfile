FROM python:3.13-slim
WORKDIR /app
COPY epub.py server.py ./
CMD ["python", "-u", "server.py"]
