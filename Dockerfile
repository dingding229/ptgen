# syntax=docker/dockerfile:1

FROM golang:1.25-bookworm AS builder
WORKDIR /src
COPY go.mod ./
COPY main.go ./
COPY web ./web
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/ptgen-go .

FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app

# Go service runtime needs sqlite3 cli + python deps for ptgen.py engine
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests beautifulsoup4 lxml

COPY --from=builder /out/ptgen-go /app/ptgen-go
COPY ptgen.py /app/ptgen.py

ENV PTGEN_HOST=0.0.0.0 \
    PTGEN_PORT=53000 \
    PTGEN_DB=/data/ptgen.db \
    PTGEN_PY_CMD="python3 /app/ptgen.py" \
    PTGEN_ADMIN_USER=admin \
    PTGEN_ADMIN_PASSWORD=admin123 \
    PTGEN_JWT_SECRET=change-this-in-production

VOLUME ["/data"]
EXPOSE 53000

CMD ["/app/ptgen-go", "-host", "0.0.0.0", "-port", "53000", "-db", "/data/ptgen.db"]
