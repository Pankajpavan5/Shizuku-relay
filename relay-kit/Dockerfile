# Universal image — works on Fly.io, Koyeb, Railway, any VPS.
FROM python:3.12-slim
WORKDIR /app
COPY server.py ./
COPY static ./static
# Pass RELAY_TOKEN at runtime; if unset a random one is generated and printed on startup (see `docker logs`)
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "server.py"]
