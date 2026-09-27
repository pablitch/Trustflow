"""Vercel FastAPI entrypoint. Frontend and API share an origin."""
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import psycopg
from backend.app import app as operational_app

app = FastAPI(title='TrustFlow', version=operational_app.version)
app.include_router(operational_app.router, prefix='/api')
PUBLIC = Path(__file__).parent/'public'

@app.middleware('http')
async def security(request: Request, call_next):
    # HttpOnly session cookies require same-origin mutation requests.
    # Devices use their own API keys; non-browser Bearer clients do not use cookies.
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin = request.headers.get('origin')
        fetch_site = request.headers.get('sec-fetch-site')
        expected = os.getenv('APP_ORIGIN', '').rstrip('/') or str(request.base_url).rstrip('/')
        if (origin and origin != expected) or fetch_site == 'cross-site':
            return JSONResponse({'detail':'Origem não autorizada.'},status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    return response

@app.exception_handler(psycopg.IntegrityError)
async def conflict(request, exc):
    return JSONResponse({'detail':'Registro duplicado ou vínculo inválido. Atualize a página e confira os dados.'},409)

@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    # Never expose connection strings or SQL data in HTTP responses/logs.
    logging.getLogger('trustflow').error('Database error: %s', type(exc).__name__)
    return JSONResponse({'detail':'Banco indisponível ou esquema não instalado. Confira a configuração da migração.'},503)

@app.get('/', include_in_schema=False)
def index(): return FileResponse(PUBLIC/'index.html', headers={'Cache-Control':'no-store'})

@app.get('/trustflow-logo.png', include_in_schema=False)
def logo(): return FileResponse(PUBLIC/'trustflow-logo.png')

@app.get('/robots.txt', include_in_schema=False)
def robots(): return FileResponse(PUBLIC/'robots.txt')
