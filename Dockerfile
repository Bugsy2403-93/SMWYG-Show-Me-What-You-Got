FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_WORKSPACE=/data/workspace \
    PROMETHEUS_PORT=9464

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_controller.py main.py ./

RUN mkdir -p /data/workspace

EXPOSE 9464

ENTRYPOINT ["python", "main.py"]
CMD ["Create a research evidence note from the supplied question."]
