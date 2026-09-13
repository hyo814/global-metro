FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /srv/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist /srv/frontend/dist
ENV FRONTEND_DIST=/srv/frontend/dist
# gtfs/ 16GB와 .cache/ 1GB는 이미지에 넣지 않는다. 볼륨으로 붙일 것.
VOLUME ["/srv/gtfs", "/srv/.cache"]
CMD exec gunicorn -w 2 -t 120 -b 0.0.0.0:${PORT:-8000} app:app
