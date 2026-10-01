# RemoteBridge relay - raw TCP (REGISTER/CONNECT), stdlib only.
# Run on a host that exposes a public TCP port: a VPS, or Fly.io (TCP service), etc.
# Render's free web services only accept HTTP(S), so the relay can't live there.
FROM python:3.12-slim
WORKDIR /app
COPY relay.py relay_token.py ./
# Set RELAY_SECRET at run time (docker run -e / fly secrets set). Without it the relay is OPEN.
EXPOSE 6000
CMD ["python", "relay.py", "--host", "0.0.0.0", "--port", "6000"]
