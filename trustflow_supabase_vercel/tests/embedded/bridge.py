import json,os,subprocess,atexit
from pathlib import Path
_worker=None
def worker():
    global _worker
    if _worker is None:
        env={**os.environ,"TRUSTFLOW_TEST_SCHEMA":str(Path(__file__).resolve().parents[2]/"sql/01_schema.sql")}
        path=os.getenv("TRUSTFLOW_TEST_WORKER",str(Path(__file__).with_name("worker.mjs")))
        _worker=subprocess.Popen(["node",path],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,env=env)
        atexit.register(_worker.terminate)
        assert json.loads(_worker.stdout.readline()).get("ready")
    return _worker
from datetime import datetime,timezone
from decimal import Decimal
class Cursor:
    description=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,query,params=()):
        if not isinstance(query,str):query=query.as_string()
        from psycopg.types.json import Jsonb
        def default(v):
            if isinstance(v,datetime):return v.isoformat()
            if isinstance(v,Jsonb):return v.obj
            if isinstance(v,Decimal):return str(v)
            return str(v)
        proc=worker()
        proc.stdin.write(json.dumps({'query':query,'params':params},default=default)+'\n');proc.stdin.flush()
        data=json.loads(proc.stdout.readline())
        if 'error' in data:raise RuntimeError(data['error']+' QUERY: '+query)
        fields=data.get('fields',[]);self.description=[(f['name'],) for f in fields]
        self.rows=[]
        for row in data.get('rows',[]):
            vals=row if isinstance(row,list) else [row[f['name']] for f in fields]
            for i,f in enumerate(fields):
                if vals[i] is not None and f['dataTypeID'] in (1114,1184):
                    d=datetime.fromisoformat(vals[i].replace('Z','+00:00'))
                    vals[i]=d.replace(tzinfo=None) if f['dataTypeID']==1114 else d
            self.rows.append(tuple(vals))
        return self
    def fetchone(self):return self.rows.pop(0) if self.rows else None
    def fetchall(self):r=self.rows;self.rows=[];return r
    def close(self):pass
class Connection:
    def __enter__(self):self.execute('BEGIN');return self
    def __exit__(self,t,v,b):self.execute('ROLLBACK' if t else 'COMMIT')
    def execute(self,q,p=()):return Cursor().execute(q,p)
    def cursor(self):return Cursor()
    def commit(self):self.execute('COMMIT');self.execute('BEGIN')
    def rollback(self):self.execute('ROLLBACK');self.execute('BEGIN')
    def close(self):pass
def connect(*args,**kwargs):return Connection()
