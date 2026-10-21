FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY newsd ./newsd
COPY config ./config
RUN pip install --no-cache-dir .
ENV NEWSD_DATABASE_URL=sqlite:////data/newsd.db
VOLUME /data
EXPOSE 8080
CMD ["newsd", "serve", "--host", "0.0.0.0", "--port", "8080"]
