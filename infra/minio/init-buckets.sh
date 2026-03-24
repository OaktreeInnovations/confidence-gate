#!/bin/sh
set -e

until mc alias set qualora http://minio:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" 2>/dev/null; do
  echo "Waiting for MinIO..."
  sleep 2
done

mc mb --ignore-existing qualora/${MINIO_DEFAULT_BUCKET}

echo "MinIO initialization complete. Bucket '${MINIO_DEFAULT_BUCKET}' is ready."
