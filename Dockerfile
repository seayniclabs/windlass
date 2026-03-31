FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir pyyaml croniter psutil

# Copy engine
COPY windlass.py .
RUN chmod +x windlass.py

# Config and state directory
RUN mkdir -p /opt/windlass
VOLUME /opt/windlass

# Docker CLI (needed to manage host containers via mounted socket)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

ENV WINDLASS_CONFIG=/opt/windlass
ENV WINDLASS_INTERVAL=300

EXPOSE 8116

CMD ["python", "windlass.py", "--serve", "--port", "8116"]
