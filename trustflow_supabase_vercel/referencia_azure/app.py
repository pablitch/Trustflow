import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from mssql_python import connect
from pydantic import BaseModel, Field

BASE_DIR = Path('/opt/trustflow/api')
load_dotenv(BASE_DIR / '.env')

SQL_CONNECTION_STRING = os.getenv('SQL_CONNECTION_STRING')
if not SQL_CONNECTION_STRING:
    raise RuntimeError('SQL_CONNECTION_STRING não configurada.')

# Chave usada para assinar tokens de login.
# Recomendo colocar AUTH_SECRET no .env. Se não existir, usa DEVICE_API_KEY como fallback.
AUTH_SECRET = os.getenv('AUTH_SECRET') or os.getenv('DEVICE_API_KEY') or 'trustflow-dev-secret-change-me'
AUTH_TOKEN_TTL_HOURS = int(os.getenv('AUTH_TOKEN_TTL_HOURS', '12'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger('trustflow-api')

app = FastAPI(title='TrustFlow API', version='3.5.0-integrado-sem-limite')


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


BR_TZ_OFFSET = timedelta(hours=-3)


def dt_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        # O banco usa SYSUTCDATETIME(). Mantemos ISO UTC para auditoria.
        return value.isoformat() + 'Z'
    return str(value)


def hora_br(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        # Exibição operacional no horário de Brasília.
        return (value + BR_TZ_OFFSET).strftime('%H:%M')

    try:
        value_str = str(value).replace('Z', '')
        dt = datetime.fromisoformat(value_str)
        return (dt + BR_TZ_OFFSET).strftime('%H:%M')
    except Exception:
        return None


def row_to_dict(cursor, row) -> dict[str, Any]:
    cols = [c[0] for c in cursor.description]
    return dict(zip(cols, row))


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [row_to_dict(cursor, r) for r in rows]


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return row_to_dict(cursor, row) if row else None


def exec_sql(sql: str, params: tuple[Any, ...] = ()) -> None:
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit()


def money(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def duracao_formatada(inicio: Any, fim: Any = None) -> str:
    if not isinstance(inicio, datetime):
        return '—'
    fim_dt = fim if isinstance(fim, datetime) else utc_now()
    total = max(0, int((fim_dt - inicio).total_seconds()))
    horas = total // 3600
    minutos = (total % 3600) // 60
    if horas <= 0:
        return f'{minutos}min'
    return f'{horas}h {minutos:02d}min'


def status_msg(status: str) -> str:
    return {
        'OK': 'Temperatura dentro do limite operacional.',
        'ATENCAO': 'Temperatura levemente acima do limite do produto.',
        'IMINENTE': 'Risco térmico iminente. Avaliar intervenção logística.',
        'CRITICO': 'Quebra térmica detectada. Smart contract deve bloquear pagamento.'
    }.get(status or 'OK', 'Status térmico indisponível.')


def calcular_status_temperatura(temp: float | None, limite: float | None) -> tuple[str, bool]:
    if temp is None or limite is None:
        return 'OK', False
    diff = temp - limite
    if diff >= 2:
        return 'CRITICO', True
    if diff >= 1:
        return 'IMINENTE', False
    if diff > 0:
        return 'ATENCAO', False
    return 'OK', False


def historico_frete(
    frete_id: int,
    sensor_id: str,
    inicio_em: Any
) -> list[dict[str, Any]]:
    historico: list[dict[str, Any]] = []

    # Ponto inicial artificial do frete:
    # o gráfico sempre nasce em 0 °C no horário real de início do frete.
    if isinstance(inicio_em, datetime):
        historico.append({
            'ts': dt_iso(inicio_em),
            'hora': hora_br(inicio_em),
            'v': 0,
            'umidade': None,
            'inicio': True,
            'offline': False,
            'statusRede': 'INICIO_FRETE'
        })

    # Todas as leituras deste frete. Nenhuma amostra é truncada em 200 pontos.
    rows = fetch_all(
        """SELECT Id, Evento, Temperatura, Umidade, StatusRede,
                  DataDispositivo, DataRecebimento
           FROM dbo.Telemetria
           WHERE FreteId = ? AND DataRecebimento >= ?
           ORDER BY DataRecebimento ASC, Id ASC;""",
        (frete_id, inicio_em if isinstance(inicio_em, datetime) else datetime(2000, 1, 1))
    )

    for r in rows:
        if r.get('Temperatura') is None:
            continue

        status_rede = str(r.get('StatusRede') or '').upper()
        evento = str(r.get('Evento') or '').upper()
        origem_offline = (
            status_rede == 'OFFLINE_BUFFER'
            or 'OFFLINE' in status_rede
            or 'OFFLINE' in evento
        )

        recebido = r.get('DataRecebimento')
        medido = r.get('DataDispositivo')
        # Relógios ausentes/inválidos usam recebimento; relógios válidos preservam
        # a ordem de coleta mesmo quando o buffer chega atrasado ou fora de ordem.
        horario_valido = (
            isinstance(medido, datetime) and isinstance(recebido, datetime)
            and medido <= recebido + timedelta(seconds=60)
            and medido >= (inicio_em if isinstance(inicio_em, datetime) else datetime(2000, 1, 1))
        )
        quando = medido if horario_valido else recebido
        historico.append({
            'idTelemetria': int(r.get('Id') or 0),
            'ts': dt_iso(quando),
            'hora': hora_br(quando),
            'dataRecebimento': dt_iso(recebido),
            'horarioEstimado': not horario_valido,
            'v': safe_float(r.get('Temperatura')),
            'umidade': safe_float(r.get('Umidade')),
            'inicio': False,
            'offline': origem_offline,
            'statusRede': r.get('StatusRede') or 'LTE',
            'evento': r.get('Evento') or 'telemetria',
            'dataDispositivo': dt_iso(r.get('DataDispositivo'))
        })

    # Segurança: mesmo se InicioEm vier nulo por algum registro legado,
    # nunca puxa telemetria antiga apenas pelo SensorId.
    if not historico:
        historico.append({
            'ts': dt_iso(utc_now()),
            'hora': hora_br(utc_now()),
            'v': 0,
            'umidade': None,
            'inicio': True,
            'offline': False,
            'statusRede': 'INICIO_FRETE'
        })

    historico.sort(key=lambda h: (datetime.fromisoformat(h['ts'].replace('Z', '+00:00')).timestamp() if h.get('ts') else float('-inf'), h.get('idTelemetria', 0)))
    return historico


def frete_rows(status_filter: str | None = None) -> list[dict[str, Any]]:
    where = ''
    params: tuple[Any, ...] = ()
    if status_filter:
        where = 'WHERE f.Status = ?'
        params = (status_filter,)

    return fetch_all(
        f'''
        SELECT
            f.FreteId,
            f.NumeroNF,
            f.SensorId,
            f.Volume,
            f.ValorCarga,
            f.ValorUnitarioCarga,
            f.TemperaturaLimite,
            f.TemperaturaMinima,
            f.TemperaturaMaxima,
            f.DestinoTexto,
            f.Status,
            f.StatusOperacional,
            f.StatusPreditivo,
            f.TempoEstabilizacaoMinutos,
            f.Violado,
            f.InicioEm,
            f.FimEm,
            f.HashContrato,
            p.Nome AS ProdutoNome,
            p.TipoProduto,
            forn.Nome AS FornecedorNome,
            cli.Nome AS ClienteNome,
            tr.Nome AS TransportadoraNome,
            cd.Nome AS CentroNome,
            cd.Codigo AS CentroCodigo,
            cd.Cidade AS CentroCidade,
            cd.UF AS CentroUF
        FROM dbo.Fretes f
        INNER JOIN dbo.Produtos p ON p.ProdutoId = f.ProdutoId
        INNER JOIN dbo.Empresas forn ON forn.EmpresaId = f.FornecedorId
        INNER JOIN dbo.Empresas cli ON cli.EmpresaId = f.ClienteId
        INNER JOIN dbo.Empresas tr ON tr.EmpresaId = f.TransportadoraId
        LEFT JOIN dbo.CentrosDistribuicao cd ON cd.CentroId = f.CentroDistribuicaoId
        {where}
        ORDER BY f.FreteId DESC;
        ''',
        params
    )


def estatisticas_termicas(
    hist: list[dict[str, Any]],
    limite: float | None,
    lsi: float | None
) -> dict[str, Any]:
    """Pico, tempo acima do limite e faixa registrada, a partir do historico.

    O tempo acima do limite soma os intervalos reais entre leituras
    consecutivas em que a temperatura esteve acima da LSC. Leituras de
    buffer offline entram normalmente: elas aconteceram, so chegaram depois.
    """
    leituras = [
        h for h in hist
        if not h.get('inicio') and h.get('v') is not None
    ]
    if not leituras:
        return {
            'picoTemp': None,
            'temperaturaMinima': None,
            'temperaturaMaxima': None,
            'temperaturaMedia': None,
            'minutosAcima': 0,
            'minutosAbaixo': 0,
            'totalLeituras': 0,
        }

    valores = [float(h['v']) for h in leituras]

    def _quando(h: dict[str, Any]) -> datetime | None:
        bruto = h.get('ts')
        if not bruto:
            return None
        try:
            return datetime.fromisoformat(str(bruto).replace('Z', ''))
        except Exception:
            return None

    minutos_acima = 0.0
    minutos_abaixo = 0.0
    for atual, proxima in zip(leituras, leituras[1:]):
        t0, t1 = _quando(atual), _quando(proxima)
        if not t0 or not t1:
            continue
        intervalo = (t1 - t0).total_seconds() / 60.0
        # Intervalo negativo ou absurdo (> 6h) indica buraco de transmissao:
        # nao da para afirmar que a carga ficou fora de faixa esse tempo todo.
        if intervalo <= 0 or intervalo > 360:
            continue
        valor = float(atual['v'])
        if limite is not None and valor > limite:
            minutos_acima += intervalo
        if lsi is not None and valor < lsi:
            minutos_abaixo += intervalo

    return {
        'picoTemp': round(max(valores), 2),
        'temperaturaMinima': round(min(valores), 2),
        'temperaturaMaxima': round(max(valores), 2),
        'temperaturaMedia': round(sum(valores) / len(valores), 2),
        'minutosAcima': int(round(minutos_acima)),
        'minutosAbaixo': int(round(minutos_abaixo)),
        'totalLeituras': len(leituras),
    }


def montar_frete(row: dict[str, Any]) -> dict[str, Any]:
    frete_id = int(row['FreteId'])
    sensor_id = row['SensorId']
    hist = historico_frete(frete_id, sensor_id, row.get('InicioEm'))
    limite = safe_float(row.get('TemperaturaLimite'))
    if limite is None:
        limite = safe_float(row.get('TemperaturaMaxima'))
    lsi = safe_float(row.get('TemperaturaMinima'))
    status_p = row.get('StatusPreditivo') or 'OK'
    status_operacional = row.get('StatusOperacional') or 'MONITORANDO'
    tempo_estabilizacao = int(row.get('TempoEstabilizacaoMinutos') or 5)

    if status_operacional == 'ESTABILIZANDO':
        mensagem_preditiva = f'Sensor em estabilização inicial. Quebra térmica suspensa por {tempo_estabilizacao} min.'
    else:
        mensagem_preditiva = status_msg(status_p)
    destino = row.get('DestinoTexto') or row.get('CentroCodigo') or row.get('CentroNome') or row.get('ClienteNome') or 'Destino'
    origem = row.get('FornecedorNome') or 'Origem'
    rota_partes = [origem, destino]

    # Local do destino. O cadastro guarda apenas Cidade/UF do centro de
    # distribuição; não existe logradouro no banco, nem para o fornecedor
    # nem para o CD.
    cidade_destino = row.get('CentroCidade')
    uf_destino = row.get('CentroUF')
    if cidade_destino and uf_destino:
        local_destino = f'{cidade_destino}/{uf_destino}'
    else:
        local_destino = cidade_destino or uf_destino or None
    destino_completo = f'{destino} · {local_destino}' if local_destino else destino

    ultima_temp = hist[-1]['v'] if hist else None
    ultima_umidade = hist[-1].get('umidade') if hist else None

    estat = estatisticas_termicas(hist, limite, lsi)

    pontos_offline = [
        {
            'hora': h.get('hora'),
            'temperatura': h.get('v'),
            'umidade': h.get('umidade'),
            'statusRede': h.get('statusRede'),
            'ts': h.get('ts')
        }
        for h in hist
        if h.get('offline') and not h.get('inicio')
    ]

    return {
        'id': frete_id,
        'numeroNF': row.get('NumeroNF'),
        'nf': row.get('NumeroNF'),
        'rota': ' → '.join(rota_partes),
        'origem': origem,
        'destino': destino,
        'destinoCidade': cidade_destino,
        'destinoUF': uf_destino,
        'destinoLocal': local_destino,
        'destinoCompleto': destino_completo,
        'produto': row.get('ProdutoNome'),
        'tipoProduto': row.get('TipoProduto'),
        'sensor': sensor_id,
        'transportadora': row.get('TransportadoraNome'),
        'fornecedor': row.get('FornecedorNome'),
        'cliente': row.get('ClienteNome'),
        'centroDistribuicao': row.get('CentroNome'),
        'status': row.get('Status'),
        'statusViagem': row.get('Status'),
        'statusOperacional': status_operacional,
        'tempoEstabilizacaoMinutos': tempo_estabilizacao,
        'statusPreditivo': status_p,
        'mensagemPreditiva': mensagem_preditiva,
        'temViolacao': bool(row.get('Violado')),
        'normalizou': False,
        'limite': limite,
        'lsc': limite,
        'lsi': lsi,
        'temperaturaAtual': ultima_temp,
        'umidadeAtual': ultima_umidade,
        'volume': int(row.get('Volume') or 0),
        'valorUnitarioCarga': money(row.get('ValorUnitarioCarga')),
        'valorCarga': money(row.get('ValorCarga')),
        'custo': money(row.get('ValorCarga')),
        'duracaoFormatada': duracao_formatada(row.get('InicioEm'), row.get('FimEm')),
        'inicioEm': dt_iso(row.get('InicioEm')),
        'fimEm': dt_iso(row.get('FimEm')),
        'dataHora': dt_iso(row.get('InicioEm')),
        'data': str(row.get('InicioEm').date()) if isinstance(row.get('InicioEm'), datetime) else None,
        'hash': row.get('HashContrato') or '',
        'historico': hist,
        'offlineCount': len(pontos_offline),
        'pontosOffline': pontos_offline,
        'temBufferOffline': len(pontos_offline) > 0,

        # Estatisticas termicas: usadas no laudo, no CSV do historico e no
        # veredito. Antes o frontend lia picoTemp/minutosAcima e recebia
        # undefined, porque nunca foram enviados.
        'picoTemp': estat['picoTemp'],
        'temperaturaMinima': estat['temperaturaMinima'],
        'temperaturaMaxima': estat['temperaturaMaxima'],
        'temperaturaMedia': estat['temperaturaMedia'],
        'minutosAcima': estat['minutosAcima'],
        'minutosAbaixo': estat['minutosAbaixo'],
        'totalLeituras': estat['totalLeituras'],

        'liquidacao': 'BLOQUEADO' if bool(row.get('Violado')) else 'LIBERADO'
    }


# ============================================================
# AUTENTICAÇÃO / PERFIS
# Perfis:
# - admin: acesso total
# - operador: operação + cadastros
# - supply: operação, histórico, transportadoras e sensores
# ============================================================

PBKDF2_ITERATIONS = 200_000
VALID_PROFILES = {'admin', 'operador', 'supply'}


class LoginPayload(BaseModel):
    email: str
    senha: str


def normalizar_perfil(perfil: Any) -> str:
    p = str(perfil or '').strip().lower()
    if p == 'suplly':
        p = 'supply'
    if p not in VALID_PROFILES:
        return 'operador'
    return p


def auth_public_user(user: dict[str, Any]) -> dict[str, Any]:
    perfil = normalizar_perfil(user.get('Perfil') or user.get('perfil'))
    nome = user.get('Nome') or user.get('nome') or user.get('Email') or user.get('email')
    email = user.get('Email') or user.get('email')
    return {
        'id': int(user.get('UsuarioId') or user.get('id') or 0),
        'nome': nome,
        'email': email,
        'perfil': perfil,
        'role': perfil.upper()
    }


def hash_password(senha: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        'sha256',
        senha.encode('utf-8'),
        bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS
    ).hex()


def verificar_senha(senha: str, salt_hex: str, expected_hash: str) -> bool:
    calculated = hash_password(senha, salt_hex)
    return hmac.compare_digest(calculated, expected_hash or '')


def carregar_usuario_por_email(email: str) -> dict[str, Any] | None:
    return fetch_one(
        '''
        SELECT
            UsuarioId,
            Nome,
            Email,
            SenhaSalt,
            SenhaHash,
            Perfil,
            Ativo
        FROM dbo.UsuariosSistema
        WHERE LOWER(Email) = LOWER(?)
          AND Ativo = 1;
        ''',
        (email,)
    )


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def b64url_decode(value: str) -> bytes:
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode('ascii'))


def criar_token(user: dict[str, Any]) -> str:
    payload = {
        'uid': int(user['UsuarioId']),
        'email': user['Email'],
        'perfil': normalizar_perfil(user['Perfil']),
        'exp': int((utc_now() + timedelta(hours=AUTH_TOKEN_TTL_HOURS)).replace(tzinfo=timezone.utc).timestamp())
    }
    payload_b64 = b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signature = hmac.new(
        AUTH_SECRET.encode('utf-8'),
        payload_b64.encode('ascii'),
        hashlib.sha256
    ).digest()
    return payload_b64 + '.' + b64url_encode(signature)


def ler_token(token: str) -> dict[str, Any]:
    try:
        payload_b64, sig_b64 = token.split('.', 1)
        expected = hmac.new(
            AUTH_SECRET.encode('utf-8'),
            payload_b64.encode('ascii'),
            hashlib.sha256
        ).digest()
        received = b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, received):
            raise ValueError('Assinatura inválida.')

        payload = json.loads(b64url_decode(payload_b64).decode('utf-8'))

        agora = int(utc_now().replace(tzinfo=timezone.utc).timestamp())
        if int(payload.get('exp') or 0) < agora:
            raise ValueError('Token expirado.')

        return payload
    except Exception:
        raise HTTPException(status_code=401, detail='Sessão inválida ou expirada.')


def usuario_atual(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(status_code=401, detail='Faça login para acessar o TrustFlow.')

    token = authorization.split(' ', 1)[1].strip()
    payload = ler_token(token)

    user = carregar_usuario_por_email(payload.get('email') or '')
    if not user:
        raise HTTPException(status_code=401, detail='Usuário não encontrado ou inativo.')

    return auth_public_user(user)


def exigir_perfil(*perfis_permitidos: str):
    allowed = {normalizar_perfil(p) for p in perfis_permitidos}

    def _dep(user: dict[str, Any] = Depends(usuario_atual)) -> dict[str, Any]:
        if user['perfil'] not in allowed:
            raise HTTPException(status_code=403, detail='Seu perfil não tem permissão para esta ação.')
        return user

    return _dep


@app.post('/auth/login')
def login(dados: LoginPayload):
    email = (dados.email or '').strip().lower()
    senha = dados.senha or ''

    if not email or not senha:
        raise HTTPException(status_code=400, detail='Informe e-mail e senha.')

    user = carregar_usuario_por_email(email)
    if not user or not verificar_senha(senha, user.get('SenhaSalt'), user.get('SenhaHash')):
        raise HTTPException(status_code=401, detail='E-mail ou senha inválidos.')

    exec_sql(
        'UPDATE dbo.UsuariosSistema SET UltimoLoginEm = SYSUTCDATETIME() WHERE UsuarioId = ?;',
        (int(user['UsuarioId']),)
    )

    return {
        'ok': True,
        'token': criar_token(user),
        'usuario': auth_public_user(user)
    }


@app.get('/auth/me')
def me(user: dict[str, Any] = Depends(usuario_atual)):
    return {
        'ok': True,
        'usuario': user
    }



class CadastroPayload(BaseModel):
    nome: str | None = None
    lsc: float | None = None
    lsi: float | None = None
    custo: float | None = None
    bateria: float | None = None
    calibracao: str | None = None
    codigo: str | None = None
    cidade: str | None = None
    uf: str | None = None
    tipoProduto: str | None = None


class FretePayload(BaseModel):
    numeroNF: str = Field(min_length=1, max_length=50)
    fornecedor: str | None = None
    cliente: str | None = None
    destino: str | None = None
    centroDistribuicao: str | None = None
    transportadora: str
    produtoNome: str
    produtoLSC: float = Field(allow_inf_nan=False)
    produtoLSI: float | None = Field(default=None, allow_inf_nan=False)
    produtoCusto: float | None = None
    # Valor total da nota/carga, informado na ABERTURA do frete por quem despacha.
    valorTotalCarga: float | None = Field(default=None, allow_inf_nan=False)
    # Mantido apenas para compatibilidade com versões antigas do frontend.
    valorUnitarioCarga: float | None = Field(default=None, allow_inf_nan=False)
    volume: int = Field(gt=0)
    sensorId: str


class FinalizarFretePayload(BaseModel):
    # Sem campos: o valor da carga é gravado na abertura do frete e não muda na
    # finalização. Mantida só para aceitar o corpo vazio enviado pelo frontend.
    pass


class LeituraManualPayload(BaseModel):
    freteId: int
    temperatura: float
    umidade: float | None = None


@app.get('/health')
def health():
    return {'ok': True, 'service': 'trustflow-api', 'version': app.version, 'message': 'API funcionando'}


# Uma ocorrencia pendente mais recente por frete ainda ativo; historico preservado.
ALARMES_ATIVOS_SQL = '''
WITH AlertasAtivos AS (
    SELECT a.*, ROW_NUMBER() OVER (
        PARTITION BY a.FreteId ORDER BY a.CriadoEm DESC, a.AlertaId DESC
    ) AS Ordem
    FROM dbo.Alertas a
    INNER JOIN dbo.Fretes f ON f.FreteId = a.FreteId
    WHERE a.ResolvidoEm IS NULL AND f.Status = N'ATIVO'
)
SELECT a.AlertaId, a.FreteId, a.SensorId, a.Nivel, a.Tipo, a.Mensagem,
       a.ValorMedido, a.Limite, a.CriadoEm, a.ResolvidoEm,
       a.ReconhecidoEm, a.ReconhecidoPor, a.Observacao,
       f.ValorCarga, p.Nome AS ProdutoNome, tr.Nome AS TransportadoraNome
FROM AlertasAtivos a
INNER JOIN dbo.Fretes f ON f.FreteId = a.FreteId
LEFT JOIN dbo.Produtos p ON p.ProdutoId = f.ProdutoId
LEFT JOIN dbo.Empresas tr ON tr.EmpresaId = f.TransportadoraId
WHERE a.Ordem = 1 AND f.Status = N'ATIVO'
ORDER BY a.CriadoEm DESC, a.AlertaId DESC;
'''


@app.get('/dashboard')
def dashboard(user: dict[str, Any] = Depends(usuario_atual)):
    produtos = fetch_all(
        '''
        SELECT Nome, TipoProduto, TemperaturaMin, TemperaturaMax, CustoUnitario
        FROM dbo.Produtos
        WHERE Ativo = 1
        ORDER BY Nome;
        '''
    )
    empresas = fetch_all(
        '''
        SELECT Tipo, Nome
        FROM dbo.Empresas
        WHERE Ativo = 1
        ORDER BY Tipo, Nome;
        '''
    )
    centros = fetch_all(
        '''
        SELECT Codigo, Nome, Cidade, UF
        FROM dbo.CentrosDistribuicao
        WHERE Ativo = 1
        ORDER BY Nome;
        '''
    )
    dispositivos = fetch_all(
        '''
        SELECT
            d.SensorId,
            d.NomeExibicao,
            d.Tipo,
            d.Ativo,
            d.Status,
            d.Bateria,
            d.Calibracao,
            d.Ciclos,
            d.UltimaComunicacao,
            f.FreteId AS FreteAtual
        FROM dbo.Dispositivos d
        LEFT JOIN dbo.Fretes f
            ON f.SensorId = d.SensorId
           AND f.Status = N'ATIVO'
        ORDER BY d.SensorId;
        '''
    )

    ativos = [montar_frete(r) for r in frete_rows('ATIVO')]
    historico = [montar_frete(r) for r in frete_rows(None) if r.get('Status') != 'ATIVO']

    alarms_rows = fetch_all(ALARMES_ATIVOS_SQL)

    cad_fornecedores = [e['Nome'] for e in empresas if e['Tipo'] == 'Fornecedor']
    cad_clientes = [e['Nome'] for e in empresas if e['Tipo'] == 'Cliente']
    cad_transportadoras = [e['Nome'] for e in empresas if e['Tipo'] == 'Transportadora']

    fixos_ids = {r['SensorId'] for r in fetch_all('SELECT SensorId FROM dbo.RefrigeradosFixos WHERE DesvinculadoEm IS NULL;')}
    sensores = []
    sensores_disponiveis = []
    for d in dispositivos:
        tem_frete = d.get('FreteAtual') is not None
        status_sensor = d.get('Status') or ('Disponível' if d.get('Ativo') else 'Inativo')
        if tem_frete:
            status_sensor = 'Em frete'
        if d['SensorId'] in fixos_ids:
            status_sensor = 'Refrigerado fixo'
        item = {
            'id': d['SensorId'],
            'nome': d.get('NomeExibicao') or d['SensorId'],
            'tipo': d.get('Tipo') or 'Temperatura/Umidade',
            'status': status_sensor,
            'freteAtual': d.get('FreteAtual'),
            'ciclos': int(d.get('Ciclos') or 0),
            'bateria': int(float(d['Bateria'])) if d.get('Bateria') is not None else None,
            'calibracao': d.get('Calibracao') or '—',
            'ultimaComunicacao': dt_iso(d.get('UltimaComunicacao'))
        }
        sensores.append(item)
        if bool(d.get('Ativo')) and status_sensor in ('Disponível', 'Disponivel') and not tem_frete:
            sensores_disponiveis.append(item['id'])

    valor_ativos = sum(float(s.get('valorCarga') or 0) for s in ativos)
    valor_hist = sum(float(s.get('valorCarga') or 0) for s in historico)

    # Valor em risco:
    # soma do valor de carga dos fretes ATIVOS que têm alarme em aberto.
    # ALARMES_ATIVOS_SQL já devolve no máximo 1 linha por frete e só de frete
    # ATIVO; a soma roda sobre `ativos` para nunca contar frete finalizado.
    fretes_com_alarme = {
        int(a['FreteId'])
        for a in alarms_rows
        if a.get('FreteId') is not None and a.get('ResolvidoEm') is None
    }
    valor_em_risco = sum(
        float(s.get('valorCarga') or 0)
        for s in ativos
        if int(s['id']) in fretes_com_alarme
    )

    viagens_ativas = len(ativos)
    viagens_historico = len(historico)
    viagens_auditadas = viagens_ativas + viagens_historico

    todos_fretes = historico + ativos

    # Salvo pela IA:
    # cargas com alarme preditivo, mas sem quebra confirmada.
    salvo_pela_ia = sum(
        float(s.get('valorCarga') or 0)
        for s in todos_fretes
        if (s.get('statusPreditivo') in ('ATENCAO', 'IMINENTE'))
        and not s.get('temViolacao')
    )

    # Pagamento retido por SLA:
    # cargas com quebra térmica confirmada.
    pagamento_retido_sla = sum(
        float(s.get('valorCarga') or 0)
        for s in todos_fretes
        if s.get('temViolacao')
    )

    transportadoras = []
    for nome in cad_transportadoras:
        fretes_tr = [
            s for s in ativos + historico
            if s.get('transportadora') == nome
        ]

        total = len(fretes_tr)
        viol = sum(1 for s in fretes_tr if s.get('temViolacao'))
        valor_risco = sum(
            s.get('valorCarga', 0)
            for s in fretes_tr
            if s.get('temViolacao')
        )

        conformidade = max(
            0,
            round(100 - (viol / total * 100 if total else 0))
        )

        # Regra do selo:
        # mínimo de 3 viagens monitoradas e pelo menos 95% de conformidade.
        verified = bool(total >= 3 and conformidade >= 95)

        transportadoras.append({
            'nome': nome,
            'viagens': total,
            'violacoes': viol,
            'conformidade': conformidade,
            'valorRisco': valor_risco,
            'verified': verified
        })

    centros_payload = [{'codigo': c['Codigo'], 'nome': c['Nome'], 'cidade': c.get('Cidade'), 'uf': c.get('UF')} for c in centros]

    payload = {
        'usuario': user,
        'cadastros': {
            'produtos': [
                {
                    'nome': p['Nome'],
                    'tipoProduto': p.get('TipoProduto'),
                    'lsi': safe_float(p.get('TemperaturaMin')),
                    'lsc': safe_float(p.get('TemperaturaMax')),
                    'custo': money(p.get('CustoUnitario'))
                }
                for p in produtos
            ],
            'fornecedores': cad_fornecedores,
            'transportadoras': cad_transportadoras,
            'clientes': cad_clientes,
            'centrosDistribuicao': centros_payload,
            'destinos': centros_payload
        },
        'sensoresDisponiveis': sensores_disponiveis,
        'sensores': sensores,
        'shipmentsAtivos': ativos,
        'shipmentsHistorico': historico,
        'alarms': [
            {
                # Campos novos/normalizados
                'id': int(a['AlertaId']),
                'freteId': int(a.get('FreteId') or 0),
                'sensorId': a.get('SensorId'),
                'nivel': a.get('Nivel') or 'ATENCAO',
                'tipo': a.get('Tipo') or 'TEMPERATURA',
                'mensagem': a.get('Mensagem'),
                'valor': safe_float(a.get('ValorMedido')),
                'limite': safe_float(a.get('Limite')),
                'ts': dt_iso(a.get('CriadoEm')),
                'produto': a.get('ProdutoNome'),
                'transportadora': a.get('TransportadoraNome'),
                'valorCarga': money(a.get('ValorCarga')),

                # Trilha de quem viu o alarme e quando.
                'reconhecidoEm': dt_iso(a.get('ReconhecidoEm')),
                'reconhecidoPor': a.get('ReconhecidoPor'),
                'observacao': a.get('Observacao'),

                # Campos compatíveis com a tela atual de Alarmes
                'idAlarme': int(a['AlertaId']),
                'frete': int(a.get('FreteId') or 0),
                'severidade': a.get('Nivel') or 'ATENCAO',
                'dataHora': dt_iso(a.get('CriadoEm')),
                'medicaoCritica': safe_float(a.get('ValorMedido'))
            }
            for a in alarms_rows
        ],
        'transportadoras': transportadoras,
        'kpis': {
            'ativosCount': viagens_ativas,
            # Passa a contar so os NAO reconhecidos: o badge existe para dizer
            # o que ainda precisa de alguem, nao o total historico em aberto.
            'alertasRefrigeracao': len([
                a for a in alarms_rows
                if a.get('ResolvidoEm') is None and a.get('ReconhecidoEm') is None
            ]),
            'valorMonitoradoAtivos': valor_ativos,

            # Card "Valor em risco" do dashboard: carga ativa com alarme aberto.
            'valorEmRisco': valor_em_risco,

            'historicoCount': viagens_historico,
            'valorMonitoradoHistorico': valor_hist,

            # Cards da tela Histórico & Smart Contracts:
            # Salvo pela IA = alarme preditivo que não virou quebra.
            # Perda evitada = pagamento retido por SLA em frete violado.
            'valorSalvo': salvo_pela_ia,
            'valorEvitado': pagamento_retido_sla,

            'viagensAuditadas': viagens_auditadas
        },
    }

    # Filtro de dados por perfil.
    # O frontend também oculta menus, mas o backend não entrega ROI para quem não pode ver.
    if user['perfil'] == 'operador':
        payload['shipmentsHistorico'] = []
        payload['transportadoras'] = []
        payload['sensores'] = []
        payload['kpis']['historicoCount'] = 0
        payload['kpis']['valorMonitoradoHistorico'] = 0
        payload['kpis']['valorSalvo'] = 0
        payload['kpis']['valorEvitado'] = 0

    return payload


@app.get('/opcoes-frete')
def opcoes_frete(user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    data = dashboard(user)
    return data['cadastros'] | {'sensoresDisponiveis': data['sensoresDisponiveis']}


def get_empresa_id(cursor, tipo: str, nome: str) -> int:
    nome = (nome or '').strip()
    if not nome:
        nome = f'{tipo} não informado'
    cursor.execute('SELECT EmpresaId FROM dbo.Empresas WHERE Tipo = ? AND Nome = ?;', (tipo, nome))
    row = cursor.fetchone()
    if row:
        return int(row[0])
    cursor.execute('INSERT INTO dbo.Empresas (Tipo, Nome) OUTPUT INSERTED.EmpresaId VALUES (?, ?);', (tipo, nome))
    return int(cursor.fetchone()[0])


def get_produto_id(cursor, nome: str, lsi: float | None, lsc: float, custo: float) -> int:
    cursor.execute('SELECT ProdutoId FROM dbo.Produtos WHERE Nome = ?;', (nome,))
    row = cursor.fetchone()
    if row:
        return int(row[0])
    cursor.execute(
        '''
        INSERT INTO dbo.Produtos (Nome, TipoProduto, TemperaturaMin, TemperaturaMax, CustoUnitario)
        OUTPUT INSERTED.ProdutoId
        VALUES (?, N'Refrigerado', ?, ?, ?);
        ''',
        (nome, lsi, lsc, custo)
    )
    return int(cursor.fetchone()[0])


@app.post('/cadastros/{tipo}')
def salvar_cadastro(tipo: str, dados: CadastroPayload, user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    tipo = tipo.lower()
    nome = (dados.nome or '').strip()
    if not nome:
        raise HTTPException(status_code=400, detail='Nome/ID obrigatório.')

    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            if tipo == 'produto':
                if dados.lsc is None or dados.custo is None:
                    raise HTTPException(status_code=400, detail='Produto exige LSC e custo.')
                cursor.execute(
                    '''
                    INSERT INTO dbo.Produtos (Nome, TipoProduto, TemperaturaMin, TemperaturaMax, CustoUnitario)
                    VALUES (?, ?, ?, ?, ?);
                    ''',
                    (nome, dados.tipoProduto or 'Refrigerado', dados.lsi, dados.lsc, dados.custo)
                )
            elif tipo in ('fornecedor', 'transportadora', 'cliente'):
                mapa = {'fornecedor': 'Fornecedor', 'transportadora': 'Transportadora', 'cliente': 'Cliente'}
                cursor.execute('INSERT INTO dbo.Empresas (Tipo, Nome) VALUES (?, ?);', (mapa[tipo], nome))
            elif tipo == 'sensor':
                sensor_id = nome.upper()
                cursor.execute(
                    '''
                    IF EXISTS (SELECT 1 FROM dbo.Dispositivos WHERE SensorId = ?)
                        UPDATE dbo.Dispositivos
                        SET NomeExibicao = ?, Bateria = COALESCE(?, Bateria), Calibracao = COALESCE(?, Calibracao), Ativo = 1,
                            Status = CASE WHEN Status IS NULL THEN N'Disponível' ELSE Status END
                        WHERE SensorId = ?
                    ELSE
                        INSERT INTO dbo.Dispositivos (SensorId, NomeExibicao, Tipo, Ativo, Status, Bateria, Calibracao, Ciclos)
                        VALUES (?, ?, N'Temperatura/Umidade', 1, N'Disponível', ?, ?, 0);
                    ''',
                    (sensor_id, sensor_id, dados.bateria, dados.calibracao, sensor_id, sensor_id, sensor_id, dados.bateria, dados.calibracao)
                )
            elif tipo in ('cd', 'centro', 'centrodistribuicao', 'destino'):
                codigo = (dados.codigo or nome).strip().upper().replace(' ', '-')
                cursor.execute(
                    '''
                    INSERT INTO dbo.CentrosDistribuicao (Codigo, Nome, Cidade, UF)
                    VALUES (?, ?, ?, ?);
                    ''',
                    (codigo, nome, dados.cidade, dados.uf)
                )
            else:
                raise HTTPException(status_code=400, detail='Tipo de cadastro inválido.')
            conn.commit()
    return {'ok': True}


@app.post('/fretes', status_code=status.HTTP_201_CREATED)
def iniciar_frete(dados: FretePayload, user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    fornecedor_nome = dados.fornecedor or 'Fornecedor padrão'
    destino_nome = dados.destino or dados.centroDistribuicao or dados.cliente or 'Destino não informado'
    centro_ref = dados.centroDistribuicao or dados.destino
    volume = int(dados.volume)
    if dados.produtoLSI is not None and dados.produtoLSI >= dados.produtoLSC:
        raise HTTPException(status_code=400, detail='O limite mínimo deve ser menor que o máximo.')

    # O valor da carga é informado na ABERTURA do frete, por quem despacha
    # (admin, operador ou supply). Depois do despacho ele não é mais alterado.
    if dados.valorTotalCarga is not None:
        valor_total = float(dados.valorTotalCarga)
    elif dados.valorUnitarioCarga is not None:
        # Compatibilidade com payload antigo: valor unitário × volume.
        valor_total = float(dados.valorUnitarioCarga) * volume
    else:
        valor_total = 0.0

    if valor_total <= 0:
        raise HTTPException(
            status_code=400,
            detail='Informe o valor total da carga (maior que zero) para iniciar o frete.'
        )

    valor_unitario_estimado = valor_total / volume if volume > 0 and valor_total > 0 else 0.0
    custo_produto = float(dados.produtoCusto or valor_unitario_estimado or 0)

    lsc = float(dados.produtoLSC)
    lsi = float(dados.produtoLSI) if dados.produtoLSI is not None else None

    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT
                    d.Ativo,
                    d.Status,
                    f.FreteId
                FROM dbo.Dispositivos d WITH (UPDLOCK, HOLDLOCK)
                LEFT JOIN dbo.Fretes f
                    ON f.SensorId = d.SensorId
                   AND f.Status = N'ATIVO'
                WHERE d.SensorId = ?;
                ''',
                (dados.sensorId,)
            )
            sensor = cursor.fetchone()
            if not sensor:
                raise HTTPException(status_code=400, detail='Sensor não cadastrado.')
            if not bool(sensor[0]):
                raise HTTPException(status_code=400, detail='Sensor inativo.')
            if sensor[2] is not None:
                raise HTTPException(status_code=400, detail='Sensor já está em frete.')
            if sensor[1] not in (None, 'Disponível', 'Disponivel'):
                raise HTTPException(status_code=400, detail=f'Sensor não disponível. Status atual: {sensor[1]}')

            cursor.execute('SELECT 1 FROM dbo.RefrigeradosFixos WHERE SensorId = ? AND DesvinculadoEm IS NULL;', (dados.sensorId,))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail='Sensor vinculado a refrigerado fixo.')

            fornecedor_id = get_empresa_id(cursor, 'Fornecedor', fornecedor_nome)
            cliente_id = get_empresa_id(cursor, 'Cliente', destino_nome)
            transportadora_id = get_empresa_id(cursor, 'Transportadora', dados.transportadora)
            produto_id = get_produto_id(cursor, dados.produtoNome, lsi, lsc, custo_produto)

            centro_id = None
            destino_texto = destino_nome
            if centro_ref:
                cursor.execute('SELECT CentroId, Nome FROM dbo.CentrosDistribuicao WHERE Codigo = ? OR Nome = ?;', (centro_ref, centro_ref))
                row = cursor.fetchone()
                if row:
                    centro_id = int(row[0])
                    destino_texto = row[1]

            base_hash = f'{dados.numeroNF}|{dados.sensorId}|{dados.produtoNome}|{valor_total}|{utc_now().isoformat()}'
            hash_contrato = hashlib.sha256(base_hash.encode('utf-8')).hexdigest()

            cursor.execute(
                '''
                INSERT INTO dbo.Fretes (
                    NumeroNF,
                    FornecedorId,
                    ClienteId,
                    TransportadoraId,
                    CentroDistribuicaoId,
                    ProdutoId,
                    SensorId,
                    Volume,
                    ValorCarga,
                    ValorUnitarioCarga,
                    TemperaturaLimite,
                    TemperaturaMinima,
                    TemperaturaMaxima,
                    DestinoTexto,
                    Status,
                    StatusPreditivo,
                    StatusOperacional,
                    Violado,
                    TempoEstabilizacaoMinutos,
                    HashContrato
                )
                OUTPUT INSERTED.FreteId
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, N'ATIVO', N'OK', N'ESTABILIZANDO', 0, 5, ?);
                ''',
                (
                    dados.numeroNF,
                    fornecedor_id,
                    cliente_id,
                    transportadora_id,
                    centro_id,
                    produto_id,
                    dados.sensorId,
                    volume,
                    valor_total,
                    valor_unitario_estimado,
                    lsc,
                    lsi,
                    lsc,
                    destino_texto,
                    hash_contrato
                )
            )
            frete_id = int(cursor.fetchone()[0])

            cursor.execute(
                '''
                UPDATE dbo.Dispositivos
                SET Status = N'Em frete', Ciclos = Ciclos + 1
                WHERE SensorId = ?;
                ''',
                (dados.sensorId,)
            )

            cursor.execute(
                '''
                INSERT INTO dbo.EventosFrete (FreteId, TipoEvento, Descricao)
                VALUES (?, N'INICIO', N'Frete iniciado. Sensor bloqueado como Em frete.');
                ''',
                (frete_id,)
            )

            conn.commit()
    return frete_id


@app.post('/fretes/{frete_id}/finalizar')
def finalizar_frete(
    frete_id: int,
    dados: FinalizarFretePayload | None = None,
    user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))
):
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute('SELECT SensorId FROM dbo.Fretes WHERE FreteId = ?;', (frete_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Frete não encontrado.')

            sensor_id = row[0]

            # O valor da carga foi gravado na abertura do frete e não muda aqui.
            cursor.execute(
                '''
                UPDATE dbo.Fretes
                SET Status = N'FINALIZADO', FimEm = SYSUTCDATETIME()
                WHERE FreteId = ?;
                ''',
                (frete_id,)
            )

            cursor.execute(
                '''
                UPDATE dbo.Dispositivos
                SET Status = N'Disponível'
                WHERE SensorId = ?;
                ''',
                (sensor_id,)
            )
            cursor.execute(
                '''
                INSERT INTO dbo.EventosFrete (FreteId, TipoEvento, Descricao)
                VALUES (?, N'FIM', N'Frete finalizado. Sensor liberado para novo uso.');
                ''',
                (frete_id,)
            )
            conn.commit()
    return {'ok': True}


class ReconhecerAlertaPayload(BaseModel):
    observacao: str | None = Field(default=None, max_length=500)


@app.post('/alertas/{alerta_id}/reconhecer')
def reconhecer_alerta(
    alerta_id: int,
    dados: ReconhecerAlertaPayload | None = None,
    user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))
):
    """Registra que alguem viu o alarme. Nao resolve nem apaga o alerta."""
    observacao = (dados.observacao or '').strip() if dados else ''

    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT ReconhecidoEm FROM dbo.Alertas WITH (UPDLOCK, HOLDLOCK) WHERE AlertaId = ?;',
                (alerta_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Alerta nao encontrado.')
            if row[0] is not None:
                # Idempotente: reconhecer duas vezes nao troca o autor original.
                conn.commit()
                return {'ok': True, 'jaReconhecido': True}

            cursor.execute(
                '''
                UPDATE dbo.Alertas
                SET ReconhecidoEm = SYSUTCDATETIME(),
                    ReconhecidoPor = ?,
                    Observacao = ?
                WHERE AlertaId = ? AND ReconhecidoEm IS NULL;
                ''',
                (user['email'], observacao or None, alerta_id)
            )
            conn.commit()

    return {'ok': True, 'jaReconhecido': False}


@app.post('/sensores/{sensor_id}/reciclar')
def reciclar_sensor(sensor_id: str, user: dict[str, Any] = Depends(exigir_perfil('admin', 'supply'))):
    exec_sql(
        '''
        UPDATE dbo.Dispositivos
        SET Status = N'Disponível'
        WHERE SensorId = ?
          AND NOT EXISTS (SELECT 1 FROM dbo.RefrigeradosFixos r WHERE r.SensorId = dbo.Dispositivos.SensorId AND r.DesvinculadoEm IS NULL)
          AND NOT EXISTS (
              SELECT 1
              FROM dbo.Fretes f
              WHERE f.SensorId = dbo.Dispositivos.SensorId
                AND f.Status = N'ATIVO'
          );
        ''',
        (sensor_id,)
    )
    return {'ok': True}


@app.post('/leituras/manual')
def leitura_manual(dados: LeituraManualPayload, user: dict[str, Any] = Depends(exigir_perfil('admin'))):
    frete = fetch_one('SELECT SensorId, TemperaturaLimite FROM dbo.Fretes WHERE FreteId = ?;', (dados.freteId,))
    if not frete:
        raise HTTPException(status_code=404, detail='Frete não encontrado.')
    status_p, violado = calcular_status_temperatura(dados.temperatura, safe_float(frete.get('TemperaturaLimite')))
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO dbo.Telemetria (FreteId, SensorId, Evento, Temperatura, Umidade, StatusRede)
                OUTPUT INSERTED.Id
                VALUES (?, ?, N'manual', ?, ?, N'WEB');
                ''',
                (dados.freteId, frete['SensorId'], dados.temperatura, dados.umidade)
            )
            tel_id = int(cursor.fetchone()[0])
            cursor.execute('UPDATE dbo.Fretes SET StatusPreditivo = ?, Violado = CASE WHEN ? = 1 THEN 1 ELSE Violado END WHERE FreteId = ?;', (status_p, 1 if violado else 0, dados.freteId))
            if status_p != 'OK':
                cursor.execute(
                    '''
                    INSERT INTO dbo.Alertas (FreteId, TelemetriaId, SensorId, Nivel, Tipo, Mensagem, ValorMedido, Limite)
                    VALUES (?, ?, ?, ?, N'TEMPERATURA', ?, ?, ?);
                    ''',
                    (dados.freteId, tel_id, frete['SensorId'], status_p, status_msg(status_p), dados.temperatura, frete.get('TemperaturaLimite'))
                )
            conn.commit()
    return {'ok': True, 'id': tel_id}


@app.get('/doca')
def consultar_doca(busca: str, user: dict[str, Any] = Depends(usuario_atual)):
    termo = (busca or '').strip()
    if not termo:
        raise HTTPException(status_code=400, detail='Informe o número do frete, NF ou ID do sensor.')
    rows = frete_rows(None)
    match = None
    for row in rows:
        if (
            str(row['FreteId']) == termo
            or str(row.get('NumeroNF') or '').upper() == termo.upper()
            or str(row['SensorId']).upper() == termo.upper()
        ):
            match = row
            break
    if not match:
        raise HTTPException(status_code=404, detail='Frete não encontrado.')
    frete = montar_frete(match)
    return {'ok': True, 'frete': frete, 'violado': bool(frete.get('temViolacao')), 'picoTemp': frete['picoTemp'], 'minutosAcima': frete['minutosAcima']}


# Refrigerados fixos: vínculo permanente, independente dos contratos de frete.
class RefrigeradoFixoPayload(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    local: str = Field(min_length=1, max_length=200)
    tipo: str = Field(min_length=1, max_length=40)
    sensorId: str = Field(min_length=1, max_length=50)
    lsi: float = Field(allow_inf_nan=False)
    lsc: float = Field(allow_inf_nan=False)


def verificar_modulo_fixos():
    if not fetch_one("SELECT CASE WHEN OBJECT_ID(N'dbo.RefrigeradosFixos', N'U') IS NOT NULL AND OBJECT_ID(N'dbo.LeiturasFixas', N'U') IS NOT NULL THEN 1 ELSE 0 END AS Id;")['Id']:
        raise HTTPException(status_code=503, detail='Módulo não instalado. Execute a migração de refrigerados fixos.')


def situacao_fixo(temp, medido_em, lsi, lsc):
    if temp is None or medido_em is None:
        return 'SEM_LEITURA'
    if (utc_now() - medido_em).total_seconds() > 300:
        return 'SEM_ATUALIZACAO'
    return 'FORA_FAIXA' if temp < lsi or temp > lsc else 'OK'


@app.get('/refrigerados-fixos')
def listar_refrigerados_fixos(user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    verificar_modulo_fixos()
    rows = fetch_all("""
        SELECT r.*, t.Temperatura, t.DataRecebimento, t.MedidoEm
        FROM dbo.RefrigeradosFixos r
        OUTER APPLY (
            SELECT TOP (1) Temperatura, DataRecebimento, MedidoEm
            FROM dbo.LeiturasFixas
            WHERE RefrigeradoId = r.RefrigeradoId AND SensorId = r.SensorId
            ORDER BY MedidoEm DESC, Id DESC
        ) t
        WHERE r.DesvinculadoEm IS NULL
        ORDER BY r.Nome, r.RefrigeradoId;
    """)
    equipamentos = []
    for r in rows:
        temp = safe_float(r['Temperatura'])
        equipamentos.append({
            'id': r['RefrigeradoId'], 'nome': r['Nome'], 'local': r['Local'],
            'tipo': r['Tipo'], 'sensorId': r['SensorId'], 'posicaoPlanta': r.get('PosicaoPlanta'),
            'lsi': float(r['TemperaturaMin']), 'lsc': float(r['TemperaturaMax']),
            'temperatura': temp,
            'ultimaLeitura': dt_iso(r['MedidoEm']),
            'status': situacao_fixo(temp, r['MedidoEm'], r['TemperaturaMin'], r['TemperaturaMax'])
        })
    sensores = fetch_all("""
        SELECT d.SensorId FROM dbo.Dispositivos d
        WHERE d.Ativo = 1 AND (d.Status IN (N'Disponível', N'Disponivel') OR d.Status IS NULL)
          AND NOT EXISTS (SELECT 1 FROM dbo.Fretes f WHERE f.SensorId = d.SensorId AND f.Status = N'ATIVO')
          AND NOT EXISTS (SELECT 1 FROM dbo.RefrigeradosFixos r WHERE r.SensorId = d.SensorId AND r.DesvinculadoEm IS NULL)
        ORDER BY d.SensorId;
    """)
    return {'equipamentos': equipamentos, 'sensoresDisponiveis': [r['SensorId'] for r in sensores]}


@app.post('/refrigerados-fixos', status_code=201)
def cadastrar_refrigerado_fixo(dados: RefrigeradoFixoPayload, user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    verificar_modulo_fixos()
    nome, local, sensor_id = dados.nome.strip(), dados.local.strip(), dados.sensorId.strip().upper()
    if not nome or not local or not sensor_id or dados.lsi >= dados.lsc:
        raise HTTPException(status_code=400, detail='Preencha nome, local e sensor; o limite mínimo deve ser menor que o máximo.')
    if dados.tipo not in ('Câmara fria', 'Geladeira', 'Freezer', 'Balcão refrigerado', 'Outro'):
        raise HTTPException(status_code=400, detail='Tipo de equipamento inválido.')
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute('SELECT Ativo, Status FROM dbo.Dispositivos WITH (UPDLOCK, HOLDLOCK) WHERE SensorId = ?;', (sensor_id,))
            sensor = cursor.fetchone()
            if not sensor or not sensor[0] or sensor[1] not in (None, 'Disponível', 'Disponivel'):
                raise HTTPException(status_code=409, detail='Sensor indisponível ou não cadastrado.')
            cursor.execute("""SELECT 1 FROM dbo.Fretes WHERE SensorId = ? AND Status = N'ATIVO'
                UNION ALL SELECT 1 FROM dbo.RefrigeradosFixos WHERE SensorId = ? AND DesvinculadoEm IS NULL;""", (sensor_id, sensor_id))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail='Sensor já vinculado a frete ou refrigerado fixo.')
            cursor.execute("""INSERT INTO dbo.RefrigeradosFixos
                (Nome, Local, Tipo, SensorId, TemperaturaMin, TemperaturaMax)
                OUTPUT INSERTED.RefrigeradoId VALUES (?, ?, ?, ?, ?, ?);""",
                (nome, local, dados.tipo, sensor_id, dados.lsi, dados.lsc))
            equipamento_id = int(cursor.fetchone()[0])
            cursor.execute("UPDATE dbo.Dispositivos SET Status = N'Refrigerado fixo' WHERE SensorId = ?;", (sensor_id,))
            conn.commit()
    return {'ok': True, 'id': equipamento_id}


@app.get('/refrigerados-fixos/{equipamento_id}/historico')
def historico_refrigerado_fixo(equipamento_id: int, user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    verificar_modulo_fixos()
    equipamento = fetch_one('SELECT * FROM dbo.RefrigeradosFixos WHERE RefrigeradoId = ?;', (equipamento_id,))
    if not equipamento:
        raise HTTPException(status_code=404, detail='Equipamento não encontrado.')
    rows = fetch_all("""
        SELECT Temperatura, MedidoEm
        FROM dbo.LeiturasFixas WHERE RefrigeradoId = ? AND SensorId = ?
        ORDER BY MedidoEm ASC, Id ASC;
    """, (equipamento_id, equipamento['SensorId']))
    return {'leituras': [{'ts': dt_iso(r['MedidoEm']), 'temperatura': safe_float(r['Temperatura'])} for r in rows]}


# HTTPS + Wi-Fi para instalações fixas. Não usa o receptor TCP dos fretes.
class LeituraFixaWifiPayload(BaseModel):
    ativoId: int = Field(gt=0)
    leituraId: str = Field(min_length=16, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    coletadoEm: int = Field(ge=1577836800, le=4102444800)
    temperatura: float = Field(ge=-55, le=125, allow_inf_nan=False)
    wifiRSSI: int | None = Field(default=None, ge=-127, le=0)


def autenticar_sensor_fixo(sensor_id: str, x_device_key: str | None = Header(default=None)) -> str:
    # Uma chave por sensor; não compartilhar DEVICE_API_KEY dos fretes.
    import re
    if not re.fullmatch(r'[A-Z0-9_-]{1,50}', sensor_id):
        raise HTTPException(status_code=400, detail='ID de sensor inválido.')
    try:
        keys = json.loads(os.getenv('FIXED_DEVICE_KEYS_JSON', '{}'))
        if not isinstance(keys, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail='Configuração de sensores Wi-Fi inválida.')
    expected = keys.get(sensor_id)
    if not isinstance(expected, str) or len(expected) < 32 or not x_device_key:
        raise HTTPException(status_code=401, detail='Credencial do sensor inválida.')
    if not hmac.compare_digest(expected.encode(), x_device_key.encode()):
        raise HTTPException(status_code=401, detail='Credencial do sensor inválida.')
    return sensor_id


def validar_vinculo_fixo(cursor, sensor_id, ativo_id=None):
    # Mesmo bloqueio e ordem da abertura de frete e do cadastro de ativo.
    cursor.execute('SELECT Ativo FROM dbo.Dispositivos WITH (UPDLOCK, HOLDLOCK) WHERE SensorId = ?;', (sensor_id,))
    device = cursor.fetchone()
    if not device or not device[0]:
        raise HTTPException(status_code=409, detail='Sensor não cadastrado ou inativo.')
    cursor.execute('SELECT RefrigeradoId, CriadoEm FROM dbo.RefrigeradosFixos WHERE SensorId = ? AND DesvinculadoEm IS NULL;', (sensor_id,))
    ativo = cursor.fetchone()
    if not ativo or (ativo_id is not None and ativo_id != int(ativo[0])):
        raise HTTPException(status_code=409, detail='Sensor não vinculado a este ativo fixo.')
    cursor.execute("SELECT 1 FROM dbo.Fretes WHERE SensorId = ? AND Status = N'ATIVO';", (sensor_id,))
    if cursor.fetchone():
        raise HTTPException(status_code=409, detail='Sensor possui frete ativo; verifique os vínculos.')
    return int(ativo[0]), ativo[1]


@app.get('/iot/fixos/{sensor_id}/config')
def config_sensor_fixo(sensor_id: str, authenticated_sensor: str = Depends(autenticar_sensor_fixo)):
    verificar_modulo_fixos()
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            ativo_id, criado_em = validar_vinculo_fixo(cursor, sensor_id)
            conn.commit()
    return {'ativoId': ativo_id, 'sensorId': sensor_id, 'intervaloSegundos': 60,
            'vinculadoEm': dt_iso(criado_em)}


@app.post('/iot/fixos/{sensor_id}/leituras')
def receber_leitura_fixa(dados: LeituraFixaWifiPayload, sensor_id: str, authenticated_sensor: str = Depends(autenticar_sensor_fixo)):
    verificar_modulo_fixos()
    medido_em = datetime.fromtimestamp(dados.coletadoEm, timezone.utc).replace(tzinfo=None)
    if medido_em > utc_now() + timedelta(seconds=60):
        raise HTTPException(status_code=422, detail='Horário da medição está no futuro.')
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            ativo_id, criado_em = validar_vinculo_fixo(cursor, sensor_id, dados.ativoId)
            if medido_em < criado_em:
                raise HTTPException(status_code=422, detail='Medição anterior ao vínculo do ativo.')
            cursor.execute('SELECT Id, RefrigeradoId, Temperatura, MedidoEm FROM dbo.LeiturasFixas WHERE SensorId = ? AND LeituraId = ?;', (sensor_id, dados.leituraId))
            existing = cursor.fetchone()
            if existing:
                if int(existing[1]) != ativo_id or float(existing[2]) != dados.temperatura or existing[3] != medido_em:
                    raise HTTPException(status_code=409, detail='Identificador de leitura reutilizado com outro conteúdo.')
                conn.commit()
                return {'ok': True, 'duplicada': True, 'leituraId': dados.leituraId, 'id': int(existing[0])}
            cursor.execute('''INSERT INTO dbo.LeiturasFixas
                (RefrigeradoId, SensorId, LeituraId, Temperatura, MedidoEm, WifiRSSI)
                OUTPUT INSERTED.Id VALUES (?, ?, ?, ?, ?, ?);''',
                (ativo_id, sensor_id, dados.leituraId, dados.temperatura, medido_em, dados.wifiRSSI))
            leitura_id = int(cursor.fetchone()[0])
            cursor.execute('UPDATE dbo.Dispositivos SET UltimaComunicacao = SYSUTCDATETIME() WHERE SensorId = ?;', (sensor_id,))
            conn.commit()
    return {'ok': True, 'duplicada': False, 'leituraId': dados.leituraId, 'id': leitura_id}


class PosicaoPlantaPayload(BaseModel):
    posicao: str | None = Field(default=None, pattern=r'^P(0[1-9]|[12][0-9]|3[0-8])$')
    posicaoAnterior: str | None = Field(default=None, pattern=r'^P(0[1-9]|[12][0-9]|3[0-8])$')


@app.put('/refrigerados-fixos/{equipamento_id}/posicao')
def posicionar_ativo(equipamento_id: int, dados: PosicaoPlantaPayload,
                    user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    verificar_modulo_fixos()
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""IF @@TRANCOUNT = 0 BEGIN TRANSACTION;
                DECLARE @r INT;
                EXEC @r = sys.sp_getapplock @Resource=N'TrustFlowPlanta',
                    @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000;
                SELECT @r;""")
            if cursor.fetchone()[0] < 0:
                raise HTTPException(status_code=409, detail='Planta em atualização. Tente novamente.')
            cursor.execute('SELECT PosicaoPlanta FROM dbo.RefrigeradosFixos WHERE RefrigeradoId = ? AND DesvinculadoEm IS NULL;', (equipamento_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Ativo não encontrado.')
            if row[0] != dados.posicaoAnterior:
                raise HTTPException(status_code=409, detail='Posição alterada por outro usuário. Atualize a planta.')
            if dados.posicao:
                cursor.execute('SELECT RefrigeradoId FROM dbo.RefrigeradosFixos WHERE PosicaoPlanta = ? AND RefrigeradoId <> ?;', (dados.posicao, equipamento_id))
                if cursor.fetchone():
                    raise HTTPException(status_code=409, detail='Esta posição já possui um sensor. Atualize a planta.')
            cursor.execute('UPDATE dbo.RefrigeradosFixos SET PosicaoPlanta = ? WHERE RefrigeradoId = ?;', (dados.posicao, equipamento_id))
            conn.commit()
    return {'ok': True, 'id': equipamento_id, 'posicaoPlanta': dados.posicao}


class DesvincularSensorFixoPayload(BaseModel):
    sensorId: str = Field(min_length=1, max_length=50)


@app.post('/refrigerados-fixos/{equipamento_id}/desvincular')
def desvincular_sensor_fixo(equipamento_id: int, dados: DesvincularSensorFixoPayload,
                           user: dict[str, Any] = Depends(exigir_perfil('admin', 'operador', 'supply'))):
    verificar_modulo_fixos()
    sensor_id = dados.sensorId.strip().upper()
    with connect(SQL_CONNECTION_STRING) as conn:
        with conn.cursor() as cursor:
            # Mesma ordem de bloqueios de coleta, cadastro e abertura de frete.
            cursor.execute('SELECT Ativo FROM dbo.Dispositivos WITH (UPDLOCK, HOLDLOCK) WHERE SensorId = ?;', (sensor_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail='Sensor não encontrado.')
            cursor.execute('SELECT SensorId, DesvinculadoEm FROM dbo.RefrigeradosFixos WITH (UPDLOCK, HOLDLOCK) WHERE RefrigeradoId = ?;', (equipamento_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Ativo não encontrado.')
            if row[0] != sensor_id:
                raise HTTPException(status_code=409, detail='O sensor do ativo mudou. Atualize a página.')
            if row[1] is not None:
                conn.commit()
                return {'ok': True, 'jaDesvinculado': True}
            cursor.execute("SELECT 1 FROM dbo.Fretes WHERE SensorId = ? AND Status = N'ATIVO';", (sensor_id,))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail='Sensor com frete ativo. Confira os vínculos antes de liberar.')
            cursor.execute('UPDATE dbo.RefrigeradosFixos SET DesvinculadoEm = SYSUTCDATETIME(), PosicaoPlanta = NULL WHERE RefrigeradoId = ?;', (equipamento_id,))
            cursor.execute("UPDATE dbo.Dispositivos SET Status = N'Disponível' WHERE SensorId = ?;", (sensor_id,))
            conn.commit()
    return {'ok': True, 'sensorId': sensor_id}
