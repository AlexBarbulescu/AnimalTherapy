FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
COPY bot_config.json .
COPY project_docs.txt .
CMD ["python", "bot.py"]