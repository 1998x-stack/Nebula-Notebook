import concurrent.futures, re, subprocess, sys, urllib.request
from pathlib import Path

OUT = Path('deploy-local/wheels'); OUT.mkdir(parents=True, exist_ok=True)
pkgs = []
for line in Path('deploy-local/runtime-lock.txt').read_text().splitlines():
    line = line.strip()
    if line and not line.startswith('#'):
        name, ver = line.split('==')
        pkgs.append((name, ver))

INDEX = 'https://mirrors.aliyun.com/pypi/simple/{}/'
BASE = 'https://mirrors.aliyun.com/pypi/'
HREF = re.compile(r'href="([^"]+)"[^>]*>([^<]+\.whl)</a>')

def norm(n): return n.lower().replace('_', '-').replace('.', '-')

def pick(name, ver, html):
    cands = []
    for href, fn in HREF.findall(html):
        if not fn.startswith(f'{name.replace("-", "_")}-{ver}-') and not fn.startswith(f'{norm(name)}-{ver}-'):
            continue
        f = fn.lower()
        if 'musllinux' in f or 'macosx' in f or 'win_' in f or 'win32' in f:
            continue
        if not ('x86_64' in f or 'none-any' in f):
            continue
        if 'cp311' in f and 'manylinux' in f:
            score = 0
        elif 'abi3' in f and 'manylinux' in f and 'x86_64' in f:
            score = 1
        elif f.endswith('none-any.whl'):
            score = 2
        else:
            continue
        cands.append((score, href, fn))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], 'manylinux2014' not in x[2]))
    return cands[0]

def resolve(pkg):
    name, ver = pkg
    try:
        req = urllib.request.Request(INDEX.format(name), headers={'User-Agent':'nebula-deploy'})
        html = urllib.request.urlopen(req, timeout=45).read().decode('utf-8', 'replace')
        got = pick(name, ver, html)
        if not got:
            print(f'NO WHEEL {name}=={ver}', flush=True); return None
        score, href, fn = got
        url = href
        while url.startswith('../'):
            url = url[3:]
        if url.startswith('/pypi/'):
            url = 'https://mirrors.aliyun.com' + url
        elif not url.startswith('http'):
            url = BASE + url
        return (name, ver, url.split('#')[0], fn)
    except Exception as e:
        print(f'RESOLVE FAIL {name}=={ver}: {e}', flush=True); return None

def dl(item):
    name, ver, url, fn = item
    dest = OUT / fn
    if dest.exists() and dest.stat().st_size > 1000:
        return fn
    for attempt in range(3):
        r = subprocess.run(['curl','-sSL','--retry','2','-m','180','-o',str(dest),url])
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 1000:
            return fn
    print(f'FAIL {name}=={ver} {url}', flush=True)
    dest.unlink(missing_ok=True)
    return None

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
    items = [x for x in ex.map(resolve, pkgs) if x]
print(f'resolved {len(items)}/{len(pkgs)}', flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    ok = [x for x in ex.map(dl, items) if x]
print(f'downloaded {len(ok)}/{len(items)}', flush=True)
