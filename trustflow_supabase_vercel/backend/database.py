"""PostgreSQL connections, transaction pooler compatible; no SQL translation."""
import os
import psycopg
from fastapi import HTTPException


def connect(url=None):
    url = url or os.getenv('DATABASE_URL', '')
    if not url:
        raise HTTPException(503, 'Configure DATABASE_URL na Vercel e execute sql/01_schema.sql.')
    # The shared pooler manages server connections. Close each client after its transaction.
    return psycopg.connect(url, prepare_threshold=None, connect_timeout=10,
                           sslmode=os.getenv('DB_SSLMODE', 'require'))
