FROM python:3.12-slim

WORKDIR /app

# print/traceback 이 버퍼에 갇혀 유실되지 않도록(크래시 로그 확인용)
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
