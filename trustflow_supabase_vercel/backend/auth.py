"""Supabase Auth plus server-side role lookup. Never trusts user_metadata."""
import os
from uuid import UUID
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from backend.database import connect

router = APIRouter(prefix='/auth')
ACCESS_COOKIE = 'tf_access'
REFRESH_COOKIE = 'tf_refresh'

class LoginPayload(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    senha: str = Field(min_length=1, max_length=1024)


def auth_request(method, endpoint, *, token=None, data=None):
    url = os.getenv('SUPABASE_URL', '').rstrip('/')
    key = os.getenv('SUPABASE_PUBLISHABLE_KEY', '')
    if not url.startswith('https://') or not key:
        raise HTTPException(503, 'Configure SUPABASE_URL e SUPABASE_PUBLISHABLE_KEY na Vercel.')
    headers = {'apikey': key}
    if token: headers['Authorization'] = 'Bearer ' + token
    try:
        r = httpx.request(method, url+'/auth/v1/'+endpoint, headers=headers, json=data, timeout=12)
    except httpx.HTTPError:
        raise HTTPException(503, 'Serviço de autenticação indisponível. Tente novamente.') from None
    if r.status_code == 429:
        raise HTTPException(429, 'Muitas tentativas de acesso. Aguarde e tente novamente.')
    if r.status_code >= 500:
        raise HTTPException(503, 'Serviço de autenticação indisponível.')
    if r.status_code >= 400:
        raise HTTPException(401, 'E-mail, senha ou sessão inválidos.')
    return r.json() if r.content else {}


def profile(user):
    try: uid = UUID(user['id'])
    except (ValueError, KeyError, TypeError): raise HTTPException(401, 'Sessão inválida.') from None
    with connect() as db:
        row = db.execute('SELECT nome, perfil, ativo FROM trustflow.profiles WHERE id = %s', (uid,)).fetchone()
    if not row or not row[2] or row[1] not in ('ADMIN','OPERADOR','SUPPLY'):
        raise HTTPException(403, 'Usuário sem perfil ativo no TrustFlow. Solicite liberação ao administrador.')
    return {'id': str(uid), 'nome': row[0] or user.get('email'), 'email': user.get('email'),
            'perfil': row[1].lower(), 'role': row[1]}


def usuario_atual(request: Request):
    bearer = request.headers.get('authorization', '')
    token = bearer[7:].strip() if bearer.lower().startswith('bearer ') else request.cookies.get(ACCESS_COOKIE)
    if not token: raise HTTPException(401, 'Faça login para acessar o TrustFlow.')
    return profile(auth_request('GET', 'user', token=token))


def exigir_perfil(*roles):
    def dep(user=Depends(usuario_atual)):
        if user['perfil'] not in roles: raise HTTPException(403, 'Seu perfil não permite esta ação.')
        return user
    return dep


def set_session(response, session):
    secure = os.getenv('COOKIE_SECURE', 'true').lower() != 'false'
    response.set_cookie(ACCESS_COOKIE, session['access_token'], httponly=True, secure=secure,
                        samesite='lax', path='/api', max_age=int(session.get('expires_in',3600)))
    response.set_cookie(REFRESH_COOKIE, session['refresh_token'], httponly=True, secure=secure,
                        samesite='lax', path='/api/auth', max_age=30*86400)
    response.headers['Cache-Control'] = 'no-store'


@router.post('/login')
def login(data: LoginPayload, response: Response):
    session = auth_request('POST', 'token?grant_type=password', data={'email':data.email.strip(), 'password':data.senha})
    user = profile(session['user'])
    set_session(response, session)
    return {'ok':True, 'token':'cookie-session', 'usuario':user}


@router.get('/me')
def me(user=Depends(usuario_atual)):
    return {'ok': True, 'usuario': user}


@router.post('/refresh')
def refresh(request: Request, response: Response):
    token=request.cookies.get(REFRESH_COOKIE)
    if not token: raise HTTPException(401, 'Faça login novamente.')
    session=auth_request('POST','token?grant_type=refresh_token',data={'refresh_token':token})
    user=profile(session['user'])
    set_session(response,session)
    return {'ok':True, 'usuario':user}


@router.post('/logout')
def logout(request: Request, response: Response):
    token=request.cookies.get(ACCESS_COOKIE)
    if token:
        try: auth_request('POST','logout?scope=local',token=token)
        except HTTPException: pass
    response.delete_cookie(ACCESS_COOKIE,path='/api')
    response.delete_cookie(REFRESH_COOKIE,path='/api/auth')
    return {'ok':True}
