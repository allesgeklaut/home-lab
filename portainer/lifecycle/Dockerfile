FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
USER 1000:1000
EXPOSE 8443
CMD ["python", "-B", "/app/app.py"]
