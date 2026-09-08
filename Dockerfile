# Container image for the backend — used by Render if you pick the Docker
# runtime instead of render.yaml's native Python, or by any other host
# (a DigitalOcean droplet, Fly, Railway). Serves the dashboard too.
FROM python:3.13-slim

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# The backend reads the facility sheets from the project root (../*.csv), so
# the whole project is copied, not just backend/.
COPY . .

WORKDIR /app/backend
ENV PORT=8000 \
    DATABASE_PATH=/var/data/scarcity.db
VOLUME ["/var/data"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
