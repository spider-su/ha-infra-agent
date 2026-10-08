FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HIA_CONFIG_DIR=/etc/home-infra-agent
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY home_infra_agent ./home_infra_agent
RUN pip install --no-cache-dir . && useradd --system --uid 10001 agent
USER agent
EXPOSE 8080
CMD ["home-infra-agent"]
