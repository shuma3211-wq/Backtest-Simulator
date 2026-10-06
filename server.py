from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from bisect import bisect_right
from datetime import datetime, timezone
import csv, random, gzip, urllib.request, os, tempfile, zipfile, json

ROOT=Path(__file__).resolve().parent
USD_CSV=ROOT/'data'/'USDJPY_M5.csv'
XAU_DIR=ROOT/'data'/'xau'
DATA_URL=os.getenv('DATA_URL','').strip()
XAU_M5_URL=os.getenv('XAU_M5_URL','').strip()
app=FastAPI()


def ensure_usd():
    if USD_CSV.exists() and USD_CSV.stat().st_size > 1000:
        return
    if not DATA_URL:
        raise RuntimeError('USDJPY data is not installed. Set DATA_URL.')
    USD_CSV.parent.mkdir(parents=True, exist_ok=True)
    gz=USD_CSV.with_suffix('.csv.gz')
    urllib.request.urlretrieve(DATA_URL, gz)
    with gzip.open(gz,'rb') as src, USD_CSV.open('wb') as dst:
        while True:
            chunk=src.read(1024*1024)
            if not chunk: break
            dst.write(chunk)
    try: gz.unlink()
    except OSError: pass


def ensure_xau():
    path=XAU_DIR/'XAU_5m_data.csv'
    if path.exists() and path.stat().st_size>1000: return
    if not XAU_M5_URL: raise RuntimeError('XAU M5 data is not installed. Set XAU_M5_URL.')
    XAU_DIR.mkdir(parents=True, exist_ok=True)
    gz=XAU_DIR/'XAU_5m_data.csv.gz'
    urllib.request.urlretrieve(XAU_M5_URL,gz)
    with gzip.open(gz,'rb') as src, path.open('wb') as dst:
        while True:
            chunk=src.read(4*1024*1024)
            if not chunk: break
            dst.write(chunk)
    try: gz.unlink()
    except OSError: pass

ensure_usd()
ensure_xau()
app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')

# Each source is native OHLC data. A small sparse timestamp index keeps memory low.
class Source:
    def __init__(self, path, has_header=True, stride=2000, fmt='xau'):
        self.path=Path(path); self.has_header=has_header; self.stride=stride; self.fmt=fmt
        self.offsets=[]; self.times=[]; self.n=0
        with self.path.open('rb') as f:
            if has_header: f.readline()
            while True:
                pos=f.tell(); line=f.readline()
                if not line: break
                self.n += 1
                if (self.n-1) % stride == 0:
                    p=line.decode('utf-8','replace').strip().split(';' if self.fmt=='xau' else ',')
                    self.offsets.append(pos); self.times.append((parse_dt_xau(p[0]) if self.fmt=='xau' else parse_dt_usd(p[0],p[1])).timestamp())
    def row(self, i):
        i=max(0,min(i,self.n-1))
        block=(i//self.stride)
        start_i=block*self.stride
        with self.path.open('rb') as f:
            f.seek(self.offsets[block])
            j=start_i
            while j<=i:
                p=f.readline().decode('utf-8','replace').strip().split(';' if self.fmt=='xau' else ',')
                if not p or len(p)<6: raise RuntimeError('bad data row')
                j+=1
        return make_row(p, i, self.fmt)
    def rows(self,a,b):
        a=max(0,int(a)); b=min(self.n,int(b)); out=[]
        if a>=b:return out
        block=a//self.stride
        with self.path.open('rb') as f:
            f.seek(self.offsets[block]); j=block*self.stride
            while j<b:
                line=f.readline()
                if not line: break
                if j>=a:
                    p=line.decode('utf-8','replace').strip().split(';' if self.fmt=='xau' else ',')
                    if len(p)>=6: out.append(make_row(p,j,self.fmt))
                j+=1
        return out
    def index_at_or_before(self, epoch):
        k=bisect_right(self.times, epoch)-1
        if k<0: return 0
        i=k*self.stride
        # Scan from sparse anchor until next row would be after target.
        with self.path.open('rb') as f:
            f.seek(self.offsets[k]); j=i; best=i
            while j<self.n:
                line=f.readline()
                if not line: break
                p=line.decode('utf-8','replace').strip().split(';' if self.fmt=='xau' else ',')
                if len(p)<6: j+=1; continue
                t=(parse_dt_xau(p[0]) if self.fmt=='xau' else parse_dt_usd(p[0],p[1])).timestamp()
                if t>epoch: break
                best=j; j+=1
                if j-i>=self.stride: break
        return best


def parse_dt_xau(s):
    return datetime.strptime(s,'%Y.%m.%d %H:%M').replace(tzinfo=timezone.utc)

def parse_dt_usd(d,t):
    return datetime.strptime(d+' '+t,'%Y.%m.%d %H:%M').replace(tzinfo=timezone.utc)

def make_row(p,i,fmt='xau'):
    if fmt=='xau':
        d,t=p[0].split(' ')
        dt=parse_dt_xau(p[0]); o,h,l,c,v=p[1:6]
    else:
        d,t=p[0],p[1]
        dt=parse_dt_usd(d,t); o,h,l,c,v=p[2:7]
    return {'date':d,'time':t,'open':float(o),'high':float(h),'low':float(l),'close':float(c),'volume':float(v) if v else 0.0,'_i':i,'_ts':dt.timestamp()}

sources={'USDJPY':{'M5':None},'XAUUSD':{'M5':Source(XAU_DIR/'XAU_5m_data.csv')}}
usd=Source(USD_CSV,has_header=False,fmt='usd')
sources['USDJPY']['M5']=usd
TF={'M5':5,'M15':15,'H1':60,'H4':240}
SYMBOL_TFS={'USDJPY':['M5','M15','H1','H4'],'XAUUSD':['M5','M15','H1','H4']}


def time_str(r): return r['date']+' '+r['time']
def utc_epoch(r): return r['_ts']

def aggregate_usd(current_epoch, tf, depth):
    mins=TF[tf]; span=mins//5
    idx=usd.index_at_or_before(current_epoch)
    rs=usd.rows(max(0,idx-depth*span-span*3),idx+1)
    out=[]; cur=None; k0=None
    for r in rs:
        d=datetime.fromtimestamp(r['_ts'],timezone.utc); k=int(d.timestamp()//60)//mins
        if k!=k0:
            if cur: out.append(cur)
            cur={'date':r['date'],'time':r['time'],'open':r['open'],'high':r['high'],'low':r['low'],'close':r['close'],'volume':r['volume'],'_i':r['_i'],'_end_i':r['_i'],'_ts':r['_ts']}; k0=k
        else:
            cur['high']=max(cur['high'],r['high']); cur['low']=min(cur['low'],r['low']); cur['close']=r['close']; cur['volume']+=r['volume']; cur['_end_i']=r['_i']; cur['_ts']=r['_ts']
    if cur: out.append(cur)
    return out[-depth:]


def aggregate_xau(current_epoch,tf,depth):
    mins=TF[tf]; span=mins//5; src=sources['XAUUSD']['M5']; idx=src.index_at_or_before(current_epoch)
    rs=src.rows(max(0,idx-depth*span-span*3),idx+1); out=[]; cur=None; k0=None
    for r in rs:
        k=int(r['_ts']//60)//mins
        if k!=k0:
            if cur: out.append(cur)
            cur={'date':r['date'],'time':r['time'],'open':r['open'],'high':r['high'],'low':r['low'],'close':r['close'],'volume':r['volume'],'_i':r['_i'],'_end_i':r['_i'],'_ts':r['_ts']}; k0=k
        else:
            cur['high']=max(cur['high'],r['high']); cur['low']=min(cur['low'],r['low']); cur['close']=r['close']; cur['volume']+=r['volume']; cur['_end_i']=r['_i']; cur['_ts']=r['_ts']
    if cur: out.append(cur)
    return out[-depth:]

def view(symbol,tf,current_epoch,depth):
    return aggregate_usd(current_epoch,tf,depth) if symbol=='USDJPY' else aggregate_xau(current_epoch,tf,depth)


def session(symbol,tf,depth):
    src=sources[symbol]['M5'] if symbol=='XAUUSD' else usd
    # Leave at least ~2000 bars of replay runway.
    lo=min(src.n-2001,max(depth+100,1000)) if src.n>3000 else max(depth,100)
    hi=max(lo,src.n-2000)
    idx=random.randint(lo,hi)
    r=src.row(idx)
    cur=r['_ts']
    return cur, view(symbol,tf,cur,depth)


def current_index(symbol,tf,epoch):
    src=sources[symbol]['M5'] if symbol=='XAUUSD' else usd
    return src.index_at_or_before(epoch)

@app.get('/')
def home(): return FileResponse(ROOT/'static/index.html')

@app.get('/api/info')
def info():
    return {'symbols':SYMBOL_TFS,'source':'XAU M5 base -> M15/H1/H4 + USDJPY M5 base -> M15/H1/H4'}

@app.get('/api/session')
def api_session(symbol:str='USDJPY',tf:str='M5',depth:int=240):
    symbol=symbol.upper(); tf=tf.upper(); depth=max(40,min(int(depth),2000))
    if symbol not in SYMBOL_TFS or tf not in SYMBOL_TFS[symbol]: raise HTTPException(400,'Unsupported symbol/timeframe')
    cur,bars=session(symbol,tf,depth)
    if not bars: raise HTTPException(500,'チャートデータを作成できませんでした。')
    return {'symbol':symbol,'tf':tf,'current':cur,'current_time':time_str(bars[-1]),'bars':bars}

@app.get('/api/view')
def api_view(symbol:str='USDJPY',tf:str='M5',current:float=0,depth:int=240):
    symbol=symbol.upper(); tf=tf.upper(); depth=max(40,min(int(depth),2000))
    if symbol not in SYMBOL_TFS or tf not in SYMBOL_TFS[symbol]: raise HTTPException(400,'Unsupported symbol/timeframe')
    bars=view(symbol,tf,float(current),depth)
    return {'symbol':symbol,'tf':tf,'current':float(current),'bars':bars}

@app.get('/api/advance')
def api_advance(symbol:str='USDJPY',tf:str='M5',current:float=0,steps:int=1,side:str='',entry:float=0,tp:float=0,sl:float=0):
    symbol=symbol.upper(); tf=tf.upper(); steps=max(1,min(int(steps),200))
    if symbol not in SYMBOL_TFS or tf not in SYMBOL_TFS[symbol]: raise HTTPException(400,'Unsupported symbol/timeframe')
    src=usd if symbol=='USDJPY' else sources['XAUUSD']['M5']
    idx=src.index_at_or_before(float(current)); span=TF[tf]//5; end=min(src.n-1,idx+steps*span)
    rs=src.rows(idx+1,end+1); shown=[]; result=None; exit_price=None
    for b in rs:
        hit_tp=(side=='LONG' and b['high']>=tp) or (side=='SHORT' and b['low']<=tp)
        hit_sl=(side=='LONG' and b['low']<=sl) or (side=='SHORT' and b['high']>=sl)
        if side and (hit_tp or hit_sl):
            result='LOSS' if hit_sl else 'WIN'; exit_price=sl if hit_sl else tp; shown.append(b); break
        shown.append(b)
    if not shown: return {'bars':[],'new_current':current,'result':None}
    new_current=shown[-1]['_ts']; display=view(symbol,tf,new_current,max(1,steps+1))
    return {'bars':display,'new_current':new_current,'result':result,'exit':exit_price} if result else {'bars':display,'new_current':new_current,'result':None}
