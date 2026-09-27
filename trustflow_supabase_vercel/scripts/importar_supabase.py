"""Import one Azure snapshot into EMPTY operational tables. No destructive updates.
Dry-run is default (transaction rolled back). --apply persists the snapshot.
Every source row is also archived privately, including unmodeled tables/columns.
"""
import argparse, hashlib, json, os
from pathlib import Path
from datetime import datetime, timezone
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

TABLES=['Empresas','Produtos','CentrosDistribuicao','Dispositivos','Fretes','Telemetria','Alertas','EventosFrete','RefrigeradosFixos','LeiturasFixas']

def verified_files(folder, manifest):
    if manifest.get('format')!=1 or manifest.get('snapshot') is not True:raise ValueError('Snapshot não reconhecido.')
    result={}
    for t in manifest['tables']:
        path=(folder/t['file']).resolve()
        if path.parent!=folder.resolve() or t['name'] in result:raise ValueError('Manifest inválido.')
        digest=hashlib.sha256();count=0
        with path.open('rb') as f:
            for line in f:digest.update(line);count+=1
        if digest.hexdigest()!=t['sha256'] or count!=t['rows']:raise ValueError('Arquivo alterado/incompleto: '+t['name'])
        result[t['name']]=(t,path)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('folder',type=Path);p.add_argument('--apply',action='store_true')
    p.add_argument('--env-file',default='.env');args=p.parse_args();load_dotenv(args.env_file)
    manifest=json.loads((args.folder/'manifest.json').read_text())
    sources=verified_files(args.folder,manifest)
    required=set(TABLES[:8])
    if not required<=sources.keys():raise ValueError('Faltam tabelas operacionais: '+str(required-sources.keys()))
    with psycopg.connect(os.environ['DATABASE_URL'],prepare_threshold=None,sslmode=os.getenv('DB_SSLMODE','require')) as db:
        db.execute('SELECT pg_advisory_xact_lock(781931)')
        for name in TABLES:
            if db.execute(sql.SQL('SELECT count(*) FROM trustflow.{}').format(sql.Identifier(name))).fetchone()[0]:
                raise ValueError('Destino não está vazio: '+name+'. Use um projeto de homologação limpo; nenhum dado foi substituído.')
        db.execute('''CREATE TABLE IF NOT EXISTS trustflow.azure_archive (
            tabela text NOT NULL, linha bigint NOT NULL, payload jsonb NOT NULL,
            PRIMARY KEY(tabela,linha))''')
        db.execute('ALTER TABLE trustflow.azure_archive ENABLE ROW LEVEL SECURITY')
        db.execute('REVOKE ALL ON trustflow.azure_archive FROM public, anon, authenticated')
        if db.execute('SELECT count(*) FROM trustflow.azure_archive').fetchone()[0]:raise ValueError('Já existe um snapshot arquivado no destino.')
        # Preserve source rows verbatim, including extra columns and old authentication records.
        for name,(info,path) in sources.items():
            with path.open() as f:
                for number,line in enumerate(f,1):
                    db.execute('INSERT INTO trustflow.azure_archive VALUES (%s,%s,%s)',(name,number,Jsonb(json.loads(line))))
        for name in TABLES:
            if name not in sources:continue
            info,path=sources[name]
            cols=db.execute("SELECT column_name,data_type,is_identity FROM information_schema.columns WHERE table_schema='trustflow' AND table_name=%s ORDER BY ordinal_position",(name,)).fetchall()
            known={c[0]:c for c in cols}
            source_cols={c['name'] for c in info['columns']}
            identities={c[0] for c in cols if c[2]=='YES'}
            if name=='Dispositivos':identities.add('SensorId')
            if info['rows'] and not identities<=source_cols:
                raise ValueError('Identificador de origem incompatível em '+name+': '+str(identities-source_cols)+'. Ajuste o mapeamento antes de migrar; IDs não serão recriados.')
            common=[c[0] for c in cols if c[0] in source_cols]
            extras=source_cols-known.keys()
            if extras:print(name,': colunas preservadas em azure_archive:',', '.join(sorted(extras)))
            query=sql.SQL('INSERT INTO trustflow.{} ({}) VALUES ({})').format(sql.Identifier(name),sql.SQL(',').join(map(sql.Identifier,common)),sql.SQL(',').join(sql.Placeholder()*len(common)))
            with path.open() as f:
                for line in f:
                    row=json.loads(line);values=[]
                    for key in common:
                        val=row.get(key)
                        if known[key][1]=='smallint' and isinstance(val,bool):val=int(val)
                        if known[key][1]=='timestamp without time zone' and val:
                            dt=datetime.fromisoformat(val.replace('Z','+00:00'))
                            val=dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
                        values.append(val)
                    db.execute(query,values)
            count=db.execute(sql.SQL('SELECT count(*) FROM trustflow.{}').format(sql.Identifier(name))).fetchone()[0]
            if count!=info['rows']:raise ValueError('Contagem divergente: '+name)
            print(name,count,'OK')
            for col,typ,identity in cols:
                if identity=='YES':
                    seq=db.execute('SELECT pg_get_serial_sequence(%s,%s)',('trustflow."'+name+'"',col)).fetchone()[0]
                    maximum=db.execute(sql.SQL('SELECT max({}) FROM trustflow.{}').format(sql.Identifier(col),sql.Identifier(name))).fetchone()[0]
                    if args.apply and maximum is not None:db.execute('SELECT setval(%s::regclass,%s,true)',(seq,maximum))
        conflicts=db.execute('''SELECT count(*) FROM trustflow."Fretes" f JOIN trustflow."RefrigeradosFixos" r
        ON f."SensorId"=r."SensorId" WHERE f."Status"='ATIVO' AND r."DesvinculadoEm" IS NULL''').fetchone()[0]
        if conflicts:raise ValueError('Há sensores vinculados simultaneamente a frete e ativo fixo. Revise o snapshot.')
        if args.apply:db.commit();print('Snapshot importado. Usuários devem ser provisionados no Supabase Auth separadamente.')
        else:db.rollback();print('DRY RUN OK. Nenhum registro persistido. Repita com --apply para importar.')

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError) as exc:raise SystemExit(str(exc)) from None
    except Exception as exc:raise SystemExit('Importação interrompida e revertida: '+type(exc).__name__+'. Confira esquema, tipos e constraints. Credenciais não são exibidas.') from None
