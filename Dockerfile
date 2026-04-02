FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Railway volume은 /data에 마운트
ENV DATA_DIR=/data

CMD ["python", "bot.py"]
