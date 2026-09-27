"""HTTP contract tests with actual SQL execution on disposable PostgreSQL/PGlite.
Never point TEST_DATABASE_URL at an operational database.
"""
import os,json,sys,uuid
from pathlib import Path
from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['COOKIE_SECURE']='false'
os.environ['FIXED_DEVICE_KEYS_JSON']=json.dumps({'WIFI-TEST':'a'*32})
os.environ['SUPABASE_URL']='https://example.supabase.co'
os.environ['SUPABASE_PUBLISHABLE_KEY']='test-public-key'
if os.getenv('TRUSTFLOW_EMBEDDED_TEST')=='1':
    sys.path.insert(0,str(Path(__file__).parent/'embedded'))
    from bridge import connect
else:
    import psycopg
    def connect(*args,**kwargs):return psycopg.connect(os.environ['TEST_DATABASE_URL'],prepare_threshold=None)
import backend.database as database
import backend.auth as auth
import backend.app as core
from app import app
UID='f869eea3-2236-43fc-9393-64fa0e6a2019'
USER={'id':UID,'email':'test@example.com','nome':'Teste','perfil':'admin','role':'ADMIN'}

@pytest.fixture(autouse=True)
def db_and_auth(monkeypatch):
    if not os.getenv('TEST_DATABASE_URL') and not os.getenv('TRUSTFLOW_EMBEDDED_TEST'):pytest.skip('Requires disposable PostgreSQL')
    monkeypatch.setattr(database,'connect',connect);monkeypatch.setattr(auth,'connect',connect);monkeypatch.setattr(core,'connect',connect)
    tables=['EventosFrete','Alertas','Telemetria','LeiturasFixas','RefrigeradosFixos','Fretes','Dispositivos','CentrosDistribuicao','Produtos','Empresas']
    with connect() as db:
        db.execute('TRUNCATE '+','.join('trustflow."'+t+'"' for t in tables)+' RESTART IDENTITY CASCADE')
        db.execute('DELETE FROM trustflow.profiles')
        db.execute('INSERT INTO auth.users(id,email) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING',(UID,USER['email']))
        db.execute("INSERT INTO trustflow.profiles VALUES (%s,'Teste','ADMIN',true,now())",(UID,))
    def request(method,endpoint,**kwargs):
        if endpoint=='user':
            if kwargs.get('token') not in ('valid-token','renewed-token'):raise auth.HTTPException(401,'Expired')
            return {'id':UID,'email':USER['email']}
        if endpoint.startswith('token'):
            return {'access_token':'valid-token','refresh_token':'refresh-token','expires_in':3600,'user':{'id':UID,'email':USER['email']}}
        return {}
    monkeypatch.setattr(auth,'auth_request',request)

@pytest.fixture
def client():
    c=TestClient(app)
    r=c.post('/api/auth/login',json={'email':USER['email'],'senha':'sample-pass'})
    assert r.status_code==200,r.text
    return c

def sensor(c,name):
    r=c.post('/api/cadastros/sensor',json={'nome':name,'bateria':80,'calibracao':'2027-01'})
    assert r.status_code==200,r.text

def freight(c):
    sensor(c,'FREIGHT-TEST')
    r=c.post('/api/fretes',json={'numeroNF':'NF-TEST','fornecedor':'Fornecedor','cliente':'Destino','transportadora':'Transportadora','produtoNome':'Produto','produtoLSC':5,'produtoLSI':-15,'volume':10,'valorTotalCarga':1000,'sensorId':'FREIGHT-TEST'})
    assert r.status_code==201,r.text
    return r.json()

def fixed(c):
    sensor(c,'WIFI-TEST')
    r=c.post('/api/refrigerados-fixos',json={'nome':'Freezer','local':'Sala A','tipo':'Freezer','sensorId':'WIFI-TEST','lsi':-15,'lsc':5})
    assert r.status_code==201,r.text
    return r.json()['id']

def test_static_and_health_without_database(monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    c=TestClient(app)
    assert c.get('/').status_code==200
    assert c.get('/trustflow-logo.png').status_code==200
    assert c.get('/api/health').status_code==200
    assert c.get('/api/dashboard').status_code==401
    assert c.get('/api/refrigerados-fixos').status_code==401

def test_auth_http_only_roles_and_logout(client):
    assert client.get('/api/auth/me').json()['usuario']['role']=='ADMIN'
    assert client.get('/api/ready').status_code==200
    with connect() as db:db.execute('UPDATE trustflow.profiles SET ativo=false WHERE id=%s',(UID,))
    assert client.get('/api/dashboard').status_code==403
    client.post('/api/auth/logout')
    assert client.get('/api/auth/me').status_code==401

def test_cookie_flags_refresh_and_cross_site(client):
    r=client.post('/api/auth/refresh')
    assert r.status_code==200
    assert 'HttpOnly' in r.headers.get('set-cookie')
    assert r.json().get('access_token') is None
    assert client.post('/api/cadastros/sensor',headers={'Origin':'https://untrusted.example'},json={'nome':'X'}).status_code==403

def test_cadastros_and_sensor_upsert(client):
    sensor(client,'FREIGHT-TEST');sensor(client,'FREIGHT-TEST')
    for typ,data in [('produto',{'nome':'Congelado','lsi':-15,'lsc':5,'custo':10}),('fornecedor',{'nome':'F'}),('cliente',{'nome':'C'}),('transportadora',{'nome':'T'}),('cd',{'nome':'CD Norte','codigo':'CD1','cidade':'São Paulo','uf':'SP'})]:
        r=client.post('/api/cadastros/'+typ,json=data);assert r.status_code==200,r.text
    r=client.get('/api/dashboard');assert r.status_code==200,r.text
    assert len(r.json()['sensores'])==1
    assert client.get('/api/opcoes-frete').status_code==200

def test_freight_lifecycle_dock_alarms_and_no_double_release(client):
    fid=freight(client)
    for t in [4,6,8]:
        r=client.post('/api/leituras/manual',json={'freteId':fid,'temperatura':t});assert r.status_code==200,r.text
    d=client.get('/api/dashboard');assert d.status_code==200,d.text
    d=d.json();assert len(d['shipmentsAtivos'])==1
    assert d['shipmentsAtivos'][0]['totalLeituras']==3
    assert len(d['alarms'])==1
    aid=d['alarms'][0]['id']
    assert client.post(f'/api/alertas/{aid}/reconhecer',json={'observacao':'Conferido'}).status_code==200
    assert client.post(f'/api/alertas/{aid}/reconhecer',json={}).json()['jaReconhecido']
    assert client.get('/api/doca',params={'busca':'NF-TEST'}).status_code==200
    assert client.post(f'/api/fretes/{fid}/finalizar',json={}).status_code==200
    next_id=freight(client)
    r=client.post(f'/api/fretes/{fid}/finalizar',json={});assert r.json()['jaFinalizado']
    with connect() as db:
        assert db.execute('SELECT "Status" FROM trustflow."Dispositivos" WHERE "SensorId"=%s',('FREIGHT-TEST',)).fetchone()[0]=='Em frete'
    assert next_id!=fid
    assert len(client.get('/api/dashboard').json()['alarms'])==0

def test_fixed_lifecycle_history_over_200_and_floor(client):
    aid=fixed(client)
    with connect() as db:db.execute('UPDATE trustflow."RefrigeradosFixos" SET "CriadoEm"=%s WHERE "RefrigeradoId"=%s',(datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(days=1),aid))
    device=TestClient(app)
    assert device.get('/api/iot/fixos/WIFI-TEST/config').status_code==401
    headers={'X-Device-Key':'a'*32}
    assert device.get('/api/iot/fixos/WIFI-TEST/config',headers=headers).json()['ativoId']==aid
    payload={'ativoId':aid,'leituraId':'test-reading-00000001','temperatura':2,'coletadoEm':int(datetime.now(timezone.utc).timestamp()),'wifiRSSI':-60}
    r=device.post('/api/iot/fixos/WIFI-TEST/leituras',headers=headers,json=payload);assert r.status_code==200,r.text
    assert device.post('/api/iot/fixos/WIFI-TEST/leituras',headers=headers,json=payload).json()['duplicada']
    assert device.post('/api/iot/fixos/WIFI-TEST/leituras',headers=headers,json={**payload,'temperatura':3}).status_code==409
    with connect() as db:
        db.execute('''INSERT INTO trustflow."LeiturasFixas" ("RefrigeradoId","SensorId","LeituraId","Temperatura","MedidoEm")
        SELECT %s,'WIFI-TEST','bulk-'||s::text,2,now() at time zone 'UTC' FROM generate_series(1,350) s''',(aid,))
    r=client.get(f'/api/refrigerados-fixos/{aid}/historico');assert r.status_code==200,r.text
    assert len(r.json()['leituras'])==351
    r=client.get('/api/refrigerados-fixos');assert r.status_code==200,r.text
    assert r.json()['equipamentos'][0]['temperatura']==2
    r=client.put(f'/api/refrigerados-fixos/{aid}/posicao',json={'posicao':'P01','posicaoAnterior':None});assert r.status_code==200,r.text
    assert client.put(f'/api/refrigerados-fixos/{aid}/posicao',json={'posicao':'P02','posicaoAnterior':None}).status_code==409
    assert client.post(f'/api/refrigerados-fixos/{aid}/desvincular',json={'sensorId':'WIFI-TEST'}).status_code==200
    assert len(client.get(f'/api/refrigerados-fixos/{aid}/historico').json()['leituras'])==351
    assert device.post('/api/iot/fixos/WIFI-TEST/leituras',headers=headers,json=payload).status_code==409

def test_fixed_sensor_not_available_for_freight(client):
    aid=fixed(client)
    r=client.post('/api/fretes',json={'numeroNF':'NF-TEST','transportadora':'T','produtoNome':'P','produtoLSC':5,'volume':1,'valorTotalCarga':100,'sensorId':'WIFI-TEST'})
    assert r.status_code in (400,409),r.text

def test_role_cannot_inject_manual(client):
    with connect() as db:db.execute("UPDATE trustflow.profiles SET perfil='OPERADOR' WHERE id=%s",(UID,))
    assert client.post('/api/leituras/manual',json={'freteId':1,'temperatura':1}).status_code==403
    assert client.post('/api/sensores/TEST/reciclar').status_code==403

def test_freight_history_over_200(client):
    fid=freight(client)
    with connect() as db:
        db.execute('''INSERT INTO trustflow."Telemetria" ("FreteId","SensorId","Evento","Temperatura","StatusRede")
        SELECT %s,'FREIGHT-TEST','offline',2,'OFFLINE_BUFFER' FROM generate_series(1,350)''',(fid,))
    r=client.get('/api/dashboard');assert r.status_code==200,r.text
    assert r.json()['shipmentsAtivos'][0]['totalLeituras']==350
