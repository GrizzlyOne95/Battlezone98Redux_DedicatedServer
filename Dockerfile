FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN useradd -m -u 10001 bzr && mkdir -p /app/data && chown -R bzr:bzr /app
USER bzr

EXPOSE 1337/tcp
EXPOSE 1338/udp
EXPOSE 1339/udp
EXPOSE 1340-1369/udp
EXPOSE 8080/tcp

CMD ["python", "server.py", "--ws-host", "0.0.0.0", "--udp-host", "0.0.0.0", "--health-host", "0.0.0.0", "--health-port", "8080", "--state-file", "/app/data/server_state.json", "--retain-empty-lobbies"]
