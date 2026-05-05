FROM python:3.12-slim

# Node.js 22 for Claude Code CLI
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x -o setup.sh && \
    bash setup.sh && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/* setup.sh

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py setup_calendar.py ./

ENV DATA_DIR=/data

CMD ["python", "bot.py"]
