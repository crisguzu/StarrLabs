"""Fresh dependency install + initializer/web smoke; never uses an existing database."""
from pathlib import Path
import json, os, secrets, shutil, subprocess, sys, tempfile, time
ROOT=Path(__file__).resolve().parents[1]
BIN=Path(os.environ['POSTGRES_BIN'])

def main():
    work=Path(tempfile.mkdtemp(prefix='starrlabs-app-',dir=os.environ.get('STARRLABS_TEST_SCRATCH',os.environ.get('XDG_RUNTIME_DIR','/dev/shm'))));os.chmod(work,0o700)
    data,sock,deps=work/'data',work/'socket',work/'deps';sock.mkdir(mode=0o700)
    proc=None
    env={'PATH':os.environ['PATH'],'HOME':str(work),'LANG':'C.UTF-8','TMPDIR':str(work),'XDG_CACHE_HOME':str(work/'cache'),'PYTHONDONTWRITEBYTECODE':'1','PYTHON_DOTENV_DISABLED':'1','API_TOKEN':'','BRAWLSTARS_API_TOKEN':'','RESEND_API_KEY':'','TURNSTILE_SITE_KEY':'','TURNSTILE_SECRET_KEY':'','PGHOST':str(sock),'PGPORT':'55448','PGUSER':'starrlabs','PGDATABASE':'starrlabs','PGCONNECT_TIMEOUT':'2','DB_HOST':str(sock),'DB_PORT':'55448','DB_NAME':'starrlabs','DB_EXPECTED_NAME':'starrlabs','DB_USER':'starrlabs','DB_PASSWORD':'disposable-unused-local-trust','DB_TARGET':'self-hosted-postgres','DB_ALLOWED_HOSTS':str(sock),'DB_SSLMODE':'disable','DB_POOL_MIN':'1','DB_POOL_MAX':'2','SESSION_SECRET':secrets.token_urlsafe(48),'EMAIL_AUTH_SECRET':secrets.token_urlsafe(48),'SESSION_HTTPS_ONLY':'0','STARRLABS_ALLOWED_HOSTS':'127.0.0.1,localhost'}
    try:
        with (work/'pip.log').open('wb') as log:
            subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--quiet','--only-binary=:all:','--no-cache-dir','--no-compile','--target',str(deps),'-r',str(ROOT/'requirements-test.txt')],env=env,stdout=log,stderr=log,check=True,timeout=12)
        env['PYTHONPATH']=str(deps)+os.pathsep+str(ROOT)
        fixture=work/'configcheck';(fixture/'scripts').mkdir(parents=True)
        shutil.copyfile(ROOT/'scripts/generate_local_secrets.py',fixture/'scripts/generate_local_secrets.py')
        shutil.copyfile(ROOT/'.env.example',fixture/'.env')
        subprocess.run([sys.executable,str(fixture/'scripts/generate_local_secrets.py')],env=env,cwd=fixture,check=True,timeout=3)
        before=(fixture/'.env').read_bytes()
        assert b'SESSION_SECRET=\n' not in before and b'EMAIL_AUTH_SECRET=\n' not in before
        subprocess.run([sys.executable,str(fixture/'scripts/generate_local_secrets.py')],env=env,cwd=fixture,check=True,timeout=3)
        assert (fixture/'.env').read_bytes()==before
        assert (fixture/'.env').stat().st_mode & 0o777 == 0o600
        subprocess.run([str(BIN/'initdb'),'-D',str(data),'-U','starrlabs','--auth-local=trust','--auth-host=reject','--no-sync'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=8)
        log=(work/'postgres.log').open('wb')
        proc=subprocess.Popen([str(BIN/'postgres'),'-D',str(data),'-k',str(sock),'-p','55448','-c','listen_addresses=','-c','shared_buffers=8MB','-c','max_connections=8','-c','max_wal_size=32MB','-c','min_wal_size=32MB','-c','wal_level=minimal','-c','max_wal_senders=0'],env=env,stdout=log,stderr=log)
        for _ in range(30):
            if (sock/'.s.PGSQL.55448').exists():break
            if proc.poll() is not None:raise RuntimeError('Disposable database startup failed')
            time.sleep(.1)
        subprocess.run([str(BIN/'createdb'),'starrlabs'],env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=3)
        env['PYTHONPATH']=str(deps)+os.pathsep+str(ROOT)
        subprocess.run([sys.executable,str(ROOT/'scripts/init_db.py'),'--confirm-empty'],env=env,cwd=ROOT,check=True,timeout=6)
        code=r'''
import json, pathlib, importlib.metadata, requests, os
from fastapi.testclient import TestClient
blocked=[]
def deny(self, method, url, **kwargs):
    blocked.append(str(url));raise RuntimeError('External HTTP is forbidden in isolated smoke')
requests.sessions.Session.request=deny
from services.email_auth import safe_next_path
assert safe_next_path('/' + chr(92) + 'attacker.invalid') == '/'
assert safe_next_path('/ok') == '/ok'
import main
checks={}
with TestClient(main.app,base_url='http://127.0.0.1:8000') as c:
    for path in ['/','/login','/how-it-works','/us','/event']:
        response=c.get(path);checks[path]=response.status_code
        assert response.status_code==200,(path,response.status_code)
        destination=pathlib.Path('.smoke-rendered');destination.mkdir(exist_ok=True)
        (destination/(path.strip('/').replace('/','_') or 'index')).with_suffix('.html').write_text(response.text)
        policy=response.headers['content-security-policy']
        assert "img-src 'self' data:;" in policy
        assert 'cdn.jsdelivr.net' not in policy and 'cloudflareinsights' not in policy
        assert 'strict-transport-security' not in response.headers
    for path in ['/missy','/api/missy/maintenance','/auth/scid-test']:
        response=c.get(path,headers={'cf-access-authenticated-user-email':'operator@example.invalid'});checks[path]=response.status_code
        assert response.status_code in (403,404),(path,response.status_code)
    assert c.get('/',headers={'host':'untrusted.example.invalid'}).status_code==400
    assert c.post('/logout',headers={'origin':'https://attacker.example.invalid'}).status_code==403
    assert c.post('/logout').status_code==403
    assert c.get('/api/lists').status_code==401
    assert c.post('/identify',headers={'origin':'http://127.0.0.1:8000'},data={'tag':'#PYPY'}).status_code==401
    import uuid, hashlib, base64, secrets
    from itsdangerous import TimestampSigner
    alpha,beta=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    with main.get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO player_profiles(tag,name) VALUES ('#PYLQGRJC','Fictional Alpha'),('#QYLGCRJP','Fictional Beta')")
        cur.execute("INSERT INTO player_verifications(player_tag,identity_email) VALUES ('#PYLQGRJC','alpha@example.invalid'),('#QYLGCRJP','beta@example.invalid')")
        for token,email in [(alpha,'alpha@example.invalid'),(beta,'beta@example.invalid')]:
            cur.execute("INSERT INTO starrlabs_identity_sessions(id,token_digest,identity_email,expires_at) VALUES (%s,%s,%s,now()+interval '1 hour')",(str(uuid.uuid4()),hashlib.sha256(token.encode()).hexdigest(),email))
    selected=TimestampSigner(os.environ['SESSION_SECRET']).sign(base64.b64encode(json.dumps({'owner_tag':'#PYLQGRJC'}).encode())).decode()
    def headers(token):
        return {'cookie':'session='+selected+'; '+main.EMAIL_AUTH_COOKIE+'='+token,'origin':'http://127.0.0.1:8000'}
    assert c.get('/api/lists',headers=headers(beta)).status_code==403
    assert c.patch('/api/lists/123',headers=headers(beta),json={'name':'Denied'}).status_code==403
    assert c.get('/api/lists',headers=headers(alpha)).status_code==200
    with main.get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE player_profiles SET trophies=1234,highest_trophies=2345,exp_level=21 WHERE tag=%s",('#PYLQGRJC',))
    profile=c.get('/player/PYLQGRJC',headers=headers(alpha))
    assert profile.status_code==200
    pathlib.Path('.smoke-rendered/player.html').write_text(profile.text)
    assert c.post('/api/lists',headers=headers(alpha),json={'name':'Fictional private list'}).status_code==200
    art=c.get('/static/starrlabs/emblem.png')
    assert art.status_code==200 and 'image/svg+xml' in art.headers['content-type']
    assert art.headers['x-starrlabs-asset']=='original-neutral-fallback'
    other=c.get('/api/me',headers=headers(beta)).json()
    assert other['recognized'] is False and other['selected_list_id'] is None
    mine=c.get('/api/me',headers=headers(alpha)).json()
    assert mine['recognized'] is True and mine['owner_tag']=='#PYLQGRJC'
    assert c.get('/api/players?limit=100000',headers=headers(alpha)).status_code==422
    assert c.post('/api/my-club/select',headers=headers(alpha)).status_code==503
    assert c.post('/add-player',headers=headers(alpha),data={'tag':'#PYLQGRJC'}).status_code==503
    token=secrets.token_urlsafe(24)
    with main.get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM player_lists WHERE owner_tag=%s AND name=%s",('#PYLQGRJC','Fictional private list'))
        private_id=cur.fetchone()[0]
        cur.execute("UPDATE player_lists SET share_token=%s,is_public=FALSE WHERE id=%s",(token,private_id))
        cur.execute("INSERT INTO player_list_members(list_id,player_tag) VALUES (%s,%s) ON CONFLICT DO NOTHING",(private_id,'#PYLQGRJC'))
    c.cookies.clear()
    assert c.get('/api/me').json()['recognized'] is False
    assert c.get('/api/shared/'+token+'/meta').status_code==404
    assert c.get('/api/shared/'+token).status_code==404
    assert c.get('/api/shared/'+token+'/players').status_code==404
    with main.get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE player_lists SET is_public=TRUE WHERE id=%s",(private_id,))
    assert c.get('/api/shared/'+token+'/meta').status_code==200
    assert c.get('/api/shared/'+token+'/players').status_code==200
    assert c.get('/?share='+token).status_code==200
    from concurrent.futures import ThreadPoolExecutor
    from request_budget import admit_request
    from fastapi import HTTPException
    def attempt(_):
        try:
            admit_request(main.get_conn,'alpha@example.invalid','synthetic-test',2,2)
            return 200
        except HTTPException as exc:
            return exc.status_code
    assert sorted(ThreadPoolExecutor(max_workers=4).map(attempt,range(4)))==[200,200,429,429]
    main.verify_turnstile=lambda *args,**kwargs: True
    sent=[]
    main.send_login_email=lambda *args: sent.append(1) or 'offline-test'
    os.environ['EMAIL_AUTH_DAILY_SEND_LIMIT']='1'
    def send(i):
        return c.post('/api/auth/email/start',headers={'origin':'http://127.0.0.1:8000'},json={'email':f'fictional{i}@example.invalid','turnstile_token':'offline-test'}).status_code
    assert sorted(ThreadPoolExecutor(max_workers=2).map(send,range(2)))==[200,429]
    assert len(sent)==1
assert not blocked,'Unexpected external HTTP call'
versions={d.metadata['Name']:d.version for d in importlib.metadata.distributions(path=[os.environ['PYTHONPATH'].split(':')[0]])}
pathlib.Path('runtime-tested-versions.json').write_text(json.dumps(versions,indent=2)+chr(10))
print(json.dumps({'app':'PASS','checks':checks,'externalHttpCalls':0,'dependencyCount':len(versions)}))
'''
        subprocess.run([sys.executable,'-c',code],env=env,cwd=ROOT,check=True,timeout=8)
    except BaseException:
        for name in ['pip.log','postgres.log']:
            if (work/name).exists():
                text=(work/name).read_bytes()[-16384:];(ROOT/('app-smoke-'+name)).write_bytes(text);print(name+': '+text.decode(errors='replace')[-3000:],file=sys.stderr)
        raise
    finally:
        if proc is not None and proc.poll() is None:proc.terminate();proc.wait(timeout=5)
        if proc is None or proc.poll() is not None:shutil.rmtree(work);print('Owned application-test database exited; owned RAM scratch removed.')

if __name__=='__main__':main()
