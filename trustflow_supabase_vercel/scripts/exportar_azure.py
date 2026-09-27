"""Read-only consistent Azure SQL snapshot; run on VM with mssql-python installed.
Usage: python scripts/exportar_azure.py --output /safe/path/export_azure
The export contains private operational data. Do not commit it to Git.
"""
import argparse, base64, hashlib, json, os
from pathlib import Path
from datetime import datetime, date, timezone
from decimal import Decimal
from dotenv import load_dotenv

def encode(value):
    if isinstance(value,(datetime,date)): return value.isoformat()
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,(bytes,bytearray,memoryview)): return {'$binary':base64.b64encode(bytes(value)).decode()}
    return str(value)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True)
    p.add_argument('--env-file',default='/opt/trustflow/api/.env')
    args=p.parse_args()
    load_dotenv(args.env_file)
    connection=os.environ.get('SQL_CONNECTION_STRING')
    if not connection: raise SystemExit('SQL_CONNECTION_STRING não configurada.')
    out=Path(args.output)
    out.mkdir(parents=True,exist_ok=False)
    os.chmod(out,0o700)
    from mssql_python import connect
    manifest={'format':1,'created_utc':datetime.now(timezone.utc).isoformat(),'schema':'dbo','snapshot':True,'tables':[]}
    try:
        with connect(connection) as db:
            with db.cursor() as cur:
                # If not enabled, fail rather than silently create an inconsistent export.
                cur.execute('SET TRANSACTION ISOLATION LEVEL SNAPSHOT;')
                cur.execute('BEGIN TRANSACTION;')
                cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME;")
                tables=[r[0] for r in cur.fetchall()]
                for idx,table in enumerate(tables):
                    cur.execute("SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=? ORDER BY ORDINAL_POSITION;",(table,))
                    schema=[{'name':r[0],'type':r[1],'nullable':r[2]=='YES'} for r in cur.fetchall()]
                    cur.execute('SELECT * FROM dbo.['+table.replace(']',']]')+'];')
                    cols=[c[0] for c in cur.description]
                    filename=f'table_{idx:04}.jsonl'; digest=hashlib.sha256();count=0
                    with (out/filename).open('wb') as f:
                        os.chmod(out/filename,0o600)
                        while True:
                            batch=cur.fetchmany(1000)
                            if not batch:break
                            for row in batch:
                                b=(json.dumps(dict(zip(cols,row)),default=encode,ensure_ascii=False)+'\n').encode()
                                digest.update(b);f.write(b);count+=1
                    manifest['tables'].append({'name':table,'file':filename,'rows':count,'sha256':digest.hexdigest(),'columns':schema})
                    print(table,count)
            db.rollback() # read-only snapshot
        (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        print('Exportação consistente concluída. Guarde a pasta fora do repositório.')
    except Exception as exc:
        # Never echo driver exceptions: they may include credentials.
        raise SystemExit('Exportação incompleta ('+type(exc).__name__+'). Se SNAPSHOT não estiver habilitado, obtenha um backup consistente antes de migrar. Nenhuma escrita no Azure foi executada.') from None

if __name__=='__main__':main()
