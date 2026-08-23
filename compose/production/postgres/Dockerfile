FROM docker.io/pgvector/pgvector:0.8.6-pg16-bookworm

COPY ./compose/production/postgres/maintenance /usr/local/bin/maintenance
RUN chmod +x /usr/local/bin/maintenance/*
RUN mv /usr/local/bin/maintenance/* /usr/local/bin \
    && rm -rf /usr/local/bin/maintenance
