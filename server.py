from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import csv, random, gzip, urllib.request, os, tempfile
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent
CSV=ROOT/'data'/'USDJPY_M5.csv'
DATA_URL=os.getenv('DATA_URL','').strip()
app=FastAPI()

def ensure_data():
    if CSV.exists() and CSV.stat().st_size > 1000:
        return
    if not DATA_URL:
        raise RuntimeError('Historical data is not installed. Set DATA_URL to the USDJPY_M5.csv.gz URL.')
    CSV.parent.mkdir(parents=True, exist_ok=True)
    gz=CSV.with_suffix('.csv.gz')
    urllib.request.urlretrieve(DATA_URL, gz)
    with gzip.open(gz,'rb') as src, CSV.open('wb') as dst:
        while True:
            chunk=src.read(1024*1024)
            if not chunk: break
            dst.write(chunk)
    try: gz.unlink()
    except OSError: pass

ensure_data()
app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')

# Index the M5 file once; only requested rows are decoded afterwards.
off=[]
with CSV.open('rb') as f:
    first=True
    while True:
        pos=f.tell(); line=f.readline()
        if not line: break
        if first:
            first=False
            # The supplied CSV has no header; the first line is data.
        off.append(pos)
N=len(off)
TF={'M5':5,'M15':15,'H1':60,'H4':240}

def rows(a,b):
    out=[]; a=max(0,int(a)); b=min(N,int(b))
    with CSV.open('rb') as f:
        for i in range(a,b):
            f.seek(off[i]); p=next(csv.reader([f.readline().decode().strip()]))
            out.append({'date':p[0],'time':p[1],'open':float(p[2]),'high':float(p[3]),'low':float(p[4]),'close':float(p[5]),'volume':float(p[6]),'_i':i})
    return out

def dt(r): return datetime.strptime(r['date']+' '+r['time'],'%Y.%m.%d %H:%M').replace(tzinfo=timezone.utc)
def key(d,mins):
    x=int(d.timestamp()//60); return x-x%mins

def aggregate(rs,tf):
    mins=TF[tf]; out=[]; cur=None; k0=None
    for r in rs:
        k=key(dt(r),mins)
        if k!=k0:
            if cur: out.append(cur)
            d=dt(r); cur={'date':d.strftime('%Y.%m.%d'),'time':d.strftime('%H:%M'),'open':r['open'],'high':r['high'],'low':r['low'],'close':r['close'],'volume':r['volume'],'_i':r['_i'],'_end_i':r['_i']}; k0=k
        else:
            cur['high']=max(cur['high'],r['high']); cur['low']=min(cur['low'],r['low']); cur['close']=r['close']; cur['volume']+=r['volume']; cur['_end_i']=r['_i']
    if cur: out.append(cur)
    return out

def view(tf, current, depth):
    span=TF[tf]//5
    # Read enough M5 rows to produce exactly depth completed candles ending at current.
    rs=rows(max(0,current-depth*span-span*3), current+1)
    bars=aggregate(rs,tf)
    return bars[-depth:]

@app.get('/')
def home(): return FileResponse(ROOT/'static/index.html')
@app.get('/api/info')
def info():
    r0=rows(0,1)[0]; r1=rows(N-1,N)[0]
    return {'rows':N,'first':r0,'last':r1,'source_tf':'M5','timeframes':['M5','M15','H1','H4'],'m1_available':False}
@app.get('/api/session')
def session(tf:str='M5',depth:int=240):
    tf=tf.upper(); depth=max(40,min(int(depth),2000))
    if tf not in TF: raise HTTPException(400,'M1は現在のM5データからは作成できません。M5/M15/H1/H4を利用してください。')
    span=TF[tf]//5
    # Leave a generous replay runway while sampling the entire history.
    lo=max(depth*span,1000); hi=max(lo+1,N-2000*span-1)
    current=random.randint(lo,hi)
    bars=view(tf,current,depth)
    if not bars: raise HTTPException(500,'チャートデータを作成できませんでした。')
    return {'tf':tf,'current_m5':current,'current_time':rows(current,current+1)[0]['date']+' '+rows(current,current+1)[0]['time'],'bars':bars}
@app.get('/api/view')
def view_api(tf:str,current:int,depth:int=240):
    tf=tf.upper(); depth=max(40,min(int(depth),2000)); current=max(0,min(int(current),N-1))
    if tf not in TF: raise HTTPException(400,'Unsupported timeframe')
    return {'tf':tf,'current_m5':current,'bars':view(tf,current,depth)}
@app.get('/api/advance')
def advance(tf:str,current:int,steps:int=1,side:str='',entry:float=0,tp:float=0,sl:float=0):
    tf=tf.upper(); steps=max(1,min(int(steps),200)); current=max(0,min(int(current),N-1))
    if tf not in TF: raise HTTPException(400,'Unsupported timeframe')
    span=TF[tf]//5
    end=min(N-1,current+steps*span)
    rs=rows(current+1,end+1)
    bars=aggregate(rs,tf)
    shown=[]
    for b in bars:
        hit_tp=side=='LONG' and b['high']>=tp or side=='SHORT' and b['low']<=tp
        hit_sl=side=='LONG' and b['low']<=sl or side=='SHORT' and b['high']>=sl
        shown.append(b)
        if side and (hit_tp or hit_sl):
            # Conservative intrabar rule: if both are touched, SL is assumed first.
            if hit_sl: return {'bars':shown,'new_current':b['_end_i'],'result':'LOSS','exit':sl}
            return {'bars':shown,'new_current':b['_end_i'],'result':'WIN','exit':tp}
    return {'bars':shown,'new_current':shown[-1]['_end_i'] if shown else current,'result':None}
