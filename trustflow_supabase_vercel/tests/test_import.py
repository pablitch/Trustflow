import json,hashlib,sys
from pathlib import Path
import pytest
from test_system import db_and_auth,client,connect,freight,fixed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import importar_supabase as imp

def snapshot(folder):
    entries=[]
    with connect() as db:
        for i,name in enumerate(imp.TABLES):
            cur=db.execute('SELECT * FROM trustflow."'+name+'"')
            names=[c[0] for c in cur.description]
            rows=[dict(zip(names,r)) for r in cur.fetchall()]
            content=''.join(json.dumps(r,default=lambda v:v.isoformat() if hasattr(v,'isoformat') else str(v))+'\n' for r in rows).encode()
            file=f'table_{i}.jsonl';(folder/file).write_bytes(content)
            entries.append({'name':name,'file':file,'rows':len(rows),'sha256':hashlib.sha256(content).hexdigest(),'columns':[{'name':n} for n in names]})
        db.execute('TRUNCATE '+','.join('trustflow."'+n+'"' for n in imp.TABLES)+' RESTART IDENTITY CASCADE')
    manifest={'format':1,'snapshot':True,'tables':entries}
    (folder/'manifest.json').write_text(json.dumps(manifest))
    return manifest

def test_import_dry_run_apply_id_and_sequence(client,tmp_path,monkeypatch):
    fid=freight(client);fixed(client)
    client.post('/api/leituras/manual',json={'freteId':fid,'temperatura':8})
    snapshot(tmp_path)
    monkeypatch.setattr(imp.psycopg,'connect',connect)
    monkeypatch.setenv('DATABASE_URL','test-only')
    monkeypatch.setattr(sys,'argv',['importar_supabase.py',str(tmp_path)])
    imp.main()
    with connect() as db:
        assert db.execute('SELECT count(*) FROM trustflow."Fretes"').fetchone()[0]==0
        assert db.execute("SELECT to_regclass('trustflow.azure_archive')").fetchone()[0] is None
    monkeypatch.setattr(sys,'argv',['importar_supabase.py',str(tmp_path),'--apply'])
    imp.main()
    with connect() as db:
        assert db.execute('SELECT "FreteId" FROM trustflow."Fretes"').fetchone()[0]==fid
        assert db.execute('SELECT count(*) FROM trustflow.azure_archive').fetchone()[0]>0
        assert db.execute("SELECT nextval('trustflow.\"Fretes_FreteId_seq\"')").fetchone()[0]>fid
    with pytest.raises(ValueError,match='não está vazio'):imp.main()
    with connect() as db:db.execute('DROP TABLE trustflow.azure_archive')

def test_import_rejects_tampered_snapshot(client,tmp_path):
    manifest=snapshot(tmp_path)
    (tmp_path/manifest['tables'][0]['file']).write_text('{}\n')
    with pytest.raises(ValueError,match='alterado/incompleto'):imp.verified_files(tmp_path,manifest)
