from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from bisect import bisect_right
from datetime import datetime, timezone
import csv, random, gzip, urllib.request, os

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
XAU_DIR = DATA_DIR / 'xau'
USD_CSV = DATA_DIR / 'USDJPY_M5.csv'

DATA_URL = os.getenv('DATA_URL', '').strip()
XAU_URLS = {
    'M1': os.getenv('XAU_M1_URL', '').strip(),
    'M5': os.getenv('XAU_M5_URL', '').strip(),
    'M15': os.getenv('XAU_M15_URL', '').strip(),
    'H1': os.getenv('XAU_H1_URL', '').strip(),
    'H4': os.getenv('XAU_H4_URL', '').strip(),
}

XAU_FILES = {
    'M1': 'XAU_1m_data.csv',
    'M5': 'XAU_5m_data.csv',
    'M15': 'XAU_15m_data.csv',
    'H1': 'XAU_1h_data.csv',
    'H4': 'XAU_4h_data.csv',
}

SYMBOL_TFS = {
    'USDJPY': ['M5', 'M15', 'H1', 'H4'],
    'XAUUSD': ['M1', 'M5', 'M15', 'H1', 'H4'],
}
TF = {'M1': 1, 'M5': 5, 'M15': 15, 'H1': 60, 'H4': 240}

app = FastAPI()


def download_file(url, target):
    if not url:
        raise RuntimeError(f'データURLが設定されていません: {target.name}')
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(target)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def ensure_usd():
    if USD_CSV.exists() and USD_CSV.stat().st_size > 1000:
        return
    if not DATA_URL:
        raise RuntimeError('USDJPY data is not installed. Set DATA_URL.')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    gz = USD_CSV.with_suffix('.csv.gz')
    urllib.request.urlretrieve(DATA_URL, gz)
    with gzip.open(gz, 'rb') as src, USD_CSV.open('wb') as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    try:
        gz.unlink()
    except OSError:
        pass


def ensure_xau(tf):
    if tf not in XAU_FILES:
        raise RuntimeError(f'Unsupported XAU timeframe: {tf}')
    path = XAU_DIR / XAU_FILES[tf]
    if not path.exists() or path.stat().st_size <= 1000:
        download_file(XAU_URLS[tf], path)
    return path


class Source:
    """Line-offset index for a native OHLC CSV. Only a small sparse index is kept in RAM."""
    def __init__(self, path, has_header=True, stride=2000, fmt='xau'):
        self.path = Path(path)
        self.has_header = has_header
        self.stride = stride
        self.fmt = fmt
        self.offsets = []
        self.times = []
        self.n = 0
        self._build_index()

    def _build_index(self):
        with self.path.open('rb') as f:
            if self.has_header:
                f.readline()
            while True:
                pos = f.tell()
                line = f.readline()
                if not line:
                    break
                self.n += 1
                if (self.n - 1) % self.stride == 0:
                    p = line.decode('utf-8', 'replace').strip().split(';' if self.fmt == 'xau' else ',')
                    if self.fmt == 'xau':
                        ts = parse_dt_xau(p[0]).timestamp()
                    else:
                        ts = parse_dt_usd(p[0], p[1]).timestamp()
                    self.offsets.append(pos)
                    self.times.append(ts)

    def row(self, i):
        i = max(0, min(i, self.n - 1))
        block = i // self.stride
        start_i = block * self.stride
        with self.path.open('rb') as f:
            f.seek(self.offsets[block])
            j = start_i
            while j <= i:
                line = f.readline()
                if not line:
                    raise RuntimeError('Unexpected end of data')
                p = line.decode('utf-8', 'replace').strip().split(';' if self.fmt == 'xau' else ',')
                j += 1
        if len(p) < 6:
            raise RuntimeError('Bad data row')
        return make_row(p, i, self.fmt)

    def rows(self, a, b):
        a = max(0, int(a))
        b = min(self.n, int(b))
        out = []
        if a >= b:
            return out
        block = a // self.stride
        with self.path.open('rb') as f:
            f.seek(self.offsets[block])
            j = block * self.stride
            while j < b:
                line = f.readline()
                if not line:
                    break
                if j >= a:
                    p = line.decode('utf-8', 'replace').strip().split(';' if self.fmt == 'xau' else ',')
                    if len(p) >= 6:
                        out.append(make_row(p, j, self.fmt))
                j += 1
        return out

    def index_at_or_before(self, epoch):
        k = bisect_right(self.times, epoch) - 1
        if k < 0:
            return 0
        i = k * self.stride
        with self.path.open('rb') as f:
            f.seek(self.offsets[k])
            j = i
            best = i
            while j < self.n:
                line = f.readline()
                if not line:
                    break
                p = line.decode('utf-8', 'replace').strip().split(';' if self.fmt == 'xau' else ',')
                if len(p) < 6:
                    j += 1
                    continue
                t = (parse_dt_xau(p[0]) if self.fmt == 'xau' else parse_dt_usd(p[0], p[1])).timestamp()
                if t > epoch:
                    break
                best = j
                j += 1
                if j - i >= self.stride:
                    break
        return best


def parse_dt_xau(s):
    return datetime.strptime(s, '%Y.%m.%d %H:%M').replace(tzinfo=timezone.utc)


def parse_dt_usd(d, t):
    return datetime.strptime(d + ' ' + t, '%Y.%m.%d %H:%M').replace(tzinfo=timezone.utc)


def make_row(p, i, fmt='xau'):
    if fmt == 'xau':
        d, t = p[0].split(' ', 1)
        dt = parse_dt_xau(p[0])
        o, h, l, c, v = p[1:6]
    else:
        d, t = p[0], p[1]
        dt = parse_dt_usd(d, t)
        o, h, l, c, v = p[2:7]
    return {
        'date': d,
        'time': t,
        'open': float(o),
        'high': float(h),
        'low': float(l),
        'close': float(c),
        'volume': float(v) if v else 0.0,
        '_i': i,
        '_ts': dt.timestamp(),
    }


# USDJPY is kept exactly as the existing M5 source and aggregated for M15/H1/H4.
ensure_usd()
usd = Source(USD_CSV, has_header=False, fmt='usd')

# XAU sources are loaded lazily: Render downloads only the timeframe that is actually requested.
xau_sources = {}


def get_xau(tf):
    if tf not in XAU_FILES:
        raise HTTPException(400, 'Unsupported XAU timeframe')
    if tf not in xau_sources:
        xau_sources[tf] = Source(ensure_xau(tf), has_header=True, fmt='xau')
    return xau_sources[tf]


def time_str(r):
    return r['date'] + ' ' + r['time']


def aggregate_usd(current_epoch, tf, depth):
    mins = TF[tf]
    span = mins // 5
    idx = usd.index_at_or_before(current_epoch)
    rs = usd.rows(max(0, idx - depth * span - span * 3), idx + 1)
    out = []
    cur = None
    k0 = None
    for r in rs:
        d = datetime.fromtimestamp(r['_ts'], timezone.utc)
        k = int(d.timestamp() // 60) // mins
        if k != k0:
            if cur:
                out.append(cur)
            cur = {
                'date': r['date'], 'time': r['time'], 'open': r['open'],
                'high': r['high'], 'low': r['low'], 'close': r['close'],
                'volume': r['volume'], '_i': r['_i'], '_end_i': r['_i'], '_ts': r['_ts']
            }
            k0 = k
        else:
            cur['high'] = max(cur['high'], r['high'])
            cur['low'] = min(cur['low'], r['low'])
            cur['close'] = r['close']
            cur['volume'] += r['volume']
            cur['_end_i'] = r['_i']
            cur['_ts'] = r['_ts']
    if cur:
        out.append(cur)
    return out[-depth:]


def native_view(tf, current_epoch, depth):
    src = get_xau(tf)
    idx = src.index_at_or_before(current_epoch)
    return src.rows(max(0, idx - depth + 1), idx + 1)


def view(symbol, tf, current_epoch, depth):
    if symbol == 'USDJPY':
        return aggregate_usd(current_epoch, tf, depth)
    return native_view(tf, current_epoch, depth)


def session(symbol, tf, depth):
    src = usd if symbol == 'USDJPY' else get_xau(tf)
    lo = min(src.n - 2001, max(depth + 100, 1000)) if src.n > 3000 else max(depth, 100)
    hi = max(lo, src.n - 2000)
    idx = random.randint(lo, hi)
    cur = src.row(idx)['_ts']
    return cur, view(symbol, tf, cur, depth)


def current_index(symbol, tf, epoch):
    src = usd if symbol == 'USDJPY' else get_xau(tf)
    return src.index_at_or_before(epoch)


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/info')
def info():
    return {'symbols': SYMBOL_TFS, 'source': 'USDJPY M5 + native XAU M1/M5/M15/H1/H4'}


@app.get('/api/session')
def api_session(symbol: str = 'USDJPY', tf: str = 'M5', depth: int = 240):
    symbol = symbol.upper(); tf = tf.upper(); depth = max(40, min(int(depth), 2000))
    if symbol not in SYMBOL_TFS or tf not in SYMBOL_TFS[symbol]:
        raise HTTPException(400, 'Unsupported symbol/timeframe')
    try:
        cur, bars = session(symbol, tf, depth)
    except Exception as e:
        raise HTTPException(500, str(e))
    if not bars:
        raise HTTPException(500, 'チャートデータを作成できませんでした。')
    return {'symbol': symbol, 'tf': tf, 'current': cur, 'current_time': time_str(bars[-1]), 'bars': bars}


@app.get('/api/view')
def api_view(symbol: str = 'USDJPY', tf: str = 'M5', current: float = 0, depth: int = 240):
    symbol = symbol.upper(); tf = tf.upper(); depth = max(40, min(int(depth), 2000))
    if symbol not in SYMBOL_TFS or tf not in SYMBOL_TFS[symbol]:
        raise HTTPException(400, 'Unsupported symbol/timeframe')
    try:
        bars = view(symbol, tf, float(current), depth)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {'symbol': symbol, 'tf': tf, 'current': float(current), 'bars': bars}


@app.get('/api/advance')
def api_advance(symbol: str = 'USDJPY', tf: str = 'M5', current: float = 0, steps: int = 1,
                side: str = '', entry: float = 0, tp: float = 0, sl: float = 0):
    symbol = symbol.upper(); tf = tf.upper(); steps = max(1, min(int(steps), 200))
    if symbol not in SYMBOL_TFS or tf not in SYMBOL_TFS[symbol]:
        raise HTTPException(400, 'Unsupported symbol/timeframe')
    try:
        src = usd if symbol == 'USDJPY' else get_xau(tf)
        idx = current_index(symbol, tf, float(current))
        end = min(src.n - 1, idx + steps)
        rs = src.rows(idx + 1, end + 1)
        shown = []
        for b in rs:
            hit_tp = (side == 'LONG' and b['high'] >= tp) or (side == 'SHORT' and b['low'] <= tp)
            hit_sl = (side == 'LONG' and b['low'] <= sl) or (side == 'SHORT' and b['high'] >= sl)
            shown.append(b)
            if side and (hit_tp or hit_sl):
                # Conservative intrabar rule: if both are touched, SL is assumed first.
                if hit_sl:
                    return {'bars': shown, 'new_current': b['_ts'], 'result': 'LOSS', 'exit': sl}
                return {'bars': shown, 'new_current': b['_ts'], 'result': 'WIN', 'exit': tp}
        return {'bars': shown, 'new_current': shown[-1]['_ts'] if shown else current, 'result': None}
    except Exception as e:
        raise HTTPException(500, str(e))
