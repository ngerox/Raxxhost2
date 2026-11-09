#!/usr/bin/env python3
"""
RAXX HOST v6 — Multi-Language Hosting + File Hosting
Features:
  - Upload .py / .js / .html / .zip (auto-detect, incl. Flask)
  - Path-based reverse proxy: /p/<slug>/ works on Render (single port)
  - Editable port + public URL override
  - Unlimited file hosting with public URLs
  - Custom dark file pickers, orbit front styling, Dark/Light theme
"""

import os, sys, re, json, time, signal, socket, shutil, sqlite3, http.client
import secrets, subprocess, zipfile, atexit, mimetypes
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, send_file, abort, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ════════════════════ CONFIG ════════════════════
ADMIN_USERNAME = 'NGRAXX'
ADMIN_PASSWORD = 'NG1988'
DB_PATH        = 'raxx_host.db'
APPS_DIR       = 'apps_data'
FILES_DIR      = 'hosted_files'
TEMP_DIR       = 'temp_uploads'
MAX_UPLOAD_MB  = 200
APP_TYPES      = ('python', 'node', 'static', 'flask')
PROXY_METHODS  = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
HOP_HEADERS    = {'host', 'connection', 'keep-alive', 'transfer-encoding',
                  'upgrade', 'proxy-connection', 'proxy-authenticate',
                  'proxy-authorization', 'te', 'trailer', 'accept-encoding',
                  'content-length'}

for d in (APPS_DIR, FILES_DIR, TEMP_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

_running = {}

# ════════════════════ SAFE PSUTIL ════════════════════
def safe_cpu():
    if not HAS_PSUTIL: return None
    try: return psutil.cpu_percent(interval=0.1)
    except Exception: return None

def safe_memory():
    if not HAS_PSUTIL: return None
    try:
        m = psutil.virtual_memory()
        return {'total': m.total, 'used': m.used, 'percent': m.percent}
    except Exception: return None

def safe_disk():
    if not HAS_PSUTIL: return None
    try:
        d = psutil.disk_usage('/')
        return {'total': d.total, 'used': d.used, 'percent': d.percent}
    except Exception: return None

def safe_uptime():
    if not HAS_PSUTIL: return None
    try: return int(time.time() - psutil.boot_time())
    except Exception: return None

def is_pid_alive(pid):
    if not pid: return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

# ════════════════════ DATABASE ════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        entry TEXT NOT NULL,
        port INTEGER NOT NULL,
        pid INTEGER,
        status TEXT DEFAULT 'stopped',
        app_type TEXT DEFAULT 'python',
        public_url TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS hosted_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        stored_name TEXT UNIQUE NOT NULL,
        filesize INTEGER NOT NULL,
        filetype TEXT NOT NULL,
        uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    for table, col, dflt in [
        ('apps', 'app_type', "'python'"),
        ('apps', 'public_url', "''"),
    ]:
        try:
            c.execute(f'SELECT {col} FROM {table} LIMIT 1')
        except sqlite3.OperationalError:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT {dflt}')

    if not c.execute('SELECT 1 FROM users WHERE username=?', (ADMIN_USERNAME,)).fetchone():
        c.execute('INSERT INTO users (username, password_hash) VALUES (?,?)',
                  (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD)))
    conn.commit()
    conn.close()

# ════════════════════ HELPERS ════════════════════
def log_event(level, message):
    try:
        conn = get_db()
        conn.execute('INSERT INTO logs (level, message) VALUES (?,?)', (level, message))
        conn.commit()
        conn.close()
    except Exception:
        pass

def find_free_port():
    own = int(os.environ.get('PORT', '0') or 0)
    for _ in range(60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            p = s.getsockname()[1]
        if p != own:
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def port_in_use(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def human_size(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

# ════════════════════ PUBLIC URLS ════════════════════
def external_base():
    """Public origin when deployed on Render (or set manually)."""
    for key in ('RENDER_EXTERNAL_URL', 'PUBLIC_BASE_URL'):
        val = (os.environ.get(key) or '').strip().rstrip('/')
        if val:
            if not val.startswith(('http://', 'https://')):
                val = 'https://' + val
            return val
    return ''

def normalize_url(raw):
    raw = (raw or '').strip()
    if not raw:
        return ''
    if not raw.startswith(('http://', 'https://')):
        raw = 'http://' + raw
    return raw.rstrip('/')

def app_path(slug):
    return f'/p/{slug}/'

def app_url(row, host=None):
    """Public URL for an app: override → Render origin/path → local host:port."""
    base = normalize_url(row['public_url'] if 'public_url' in row.keys() else '')
    if base:
        return base
    ext = external_base()
    if ext:
        return ext + app_path(row['slug'])
    host = host or 'localhost'
    return f"http://{host}:{row['port']}/"

# ════════════════════ APP TYPE DETECTION ════════════════════
FLASK_HINTS = ('from flask import', 'import flask', 'flask.Flask(', 'Flask(__name__)')

def read_head(path, limit=24000):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(limit)
    except Exception:
        return ''

def looks_like_flask(path):
    txt = read_head(path)
    return any(h in txt for h in FLASK_HINTS)

def detect_app_type(app_dir):
    files = os.listdir(app_dir)
    if 'package.json' in files:
        try:
            with open(os.path.join(app_dir, 'package.json')) as f:
                pkg = json.load(f)
            entry = pkg.get('main', 'index.js')
            if os.path.exists(os.path.join(app_dir, entry)):
                return ('node', entry)
            return ('node', 'index.js' if 'index.js' in files else None)
        except Exception:
            return ('node', 'index.js' if 'index.js' in files else None)

    for f in ['main.py', 'app.py', 'server.py', 'run.py', 'index.py']:
        if f in files:
            full = os.path.join(app_dir, f)
            return ('flask' if looks_like_flask(full) else 'python', f)

    if 'index.html' in files:
        return ('static', '.')

    for root, dirs, fs in os.walk(app_dir):
        rel = os.path.relpath(root, app_dir)
        for f in ['main.py', 'app.py', 'index.html']:
            if f in fs:
                path = os.path.join(rel, f) if rel != '.' else f
                if f == 'index.html':
                    return ('static', rel if rel != '.' else '.')
                full = os.path.join(root, f)
                return ('flask' if looks_like_flask(full) else 'python', path)

    return (None, None)

def scan_zip_contents(app_dir):
    all_files = []
    entries = []
    total_size = 0
    max_files = 500

    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in ('node_modules', '__pycache__', '.git', 'venv')]
        rel = os.path.relpath(root, app_dir)
        for f in files:
            if f.startswith('.'): continue
            if len(all_files) >= max_files: break
            full = os.path.join(root, f)
            try:
                size = os.path.getsize(full)
                total_size += size
                path = os.path.join(rel, f) if rel != '.' else f

                if f.endswith('.py'):
                    if looks_like_flask(full):
                        all_files.append({'path': path, 'size': size, 'kind': 'flask'})
                        entries.append({'path': path, 'type': 'flask', 'label': '🧪 ' + path,
                                        'priority': 30 if f in ('main.py', 'app.py') else 15})
                    else:
                        all_files.append({'path': path, 'size': size, 'kind': 'python'})
                        entries.append({'path': path, 'type': 'python', 'label': '🐍 ' + path,
                                        'priority': 10 if f in ('main.py', 'app.py') else 1})
                elif f.endswith('.js'):
                    all_files.append({'path': path, 'size': size, 'kind': 'node'})
                    entries.append({'path': path, 'type': 'node', 'label': '⬢ ' + path,
                                    'priority': 10 if f in ('index.js', 'server.js') else 2})
                elif f == 'index.html':
                    all_files.append({'path': path, 'size': size, 'kind': 'html'})
                    html_dir = rel if rel != '.' else '.'
                    entries.append({'path': html_dir, 'type': 'static',
                                    'label': '🌐 Static site (' + html_dir + ')',
                                    'priority': 20})
                else:
                    all_files.append({'path': path, 'size': size, 'kind': 'other'})
            except Exception:
                continue

    entries.sort(key=lambda x: x['priority'], reverse=True)
    seen = set()
    unique = []
    for e in entries:
        key = (e['type'], e['path'])
        if key in seen: continue
        seen.add(key)
        unique.append(e)

    return {
        'files': all_files[:max_files],
        'entries': unique[:30],
        'total_size': total_size,
        'file_count': len(all_files)
    }

def ensure_flask():
    try:
        if subprocess.run([sys.executable, '-c', 'import flask'],
                          capture_output=True, timeout=30).returncode == 0:
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'flask', '--disable-pip-version-check', '-q'],
            capture_output=True, text=True, timeout=240
        )
        if r.returncode == 0:
            log_event('SUCCESS', 'pip installed flask')
            return True
        log_event('ERROR', f'flask install failed: {r.stderr[-200:]}')
    except Exception as e:
        log_event('ERROR', f'flask install error: {e}')
    return False

def auto_install(app_dir, app_type):
    installed = []

    if app_type == 'flask':
        if ensure_flask():
            installed.append('pip: flask')

    req = os.path.join(app_dir, 'requirements.txt')
    if os.path.exists(req) and app_type in ('python', 'flask'):
        try:
            r = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-r', req,
                 '--disable-pip-version-check', '-q'],
                capture_output=True, text=True, timeout=300
            )
            if r.returncode == 0:
                installed.append('pip: requirements.txt')
                log_event('SUCCESS', 'pip installed requirements.txt')
            else:
                log_event('ERROR', f'pip failed: {r.stderr[-200:]}')
        except Exception as e:
            log_event('ERROR', f'pip error: {e}')

    pkg = os.path.join(app_dir, 'package.json')
    if os.path.exists(pkg) and app_type == 'node':
        try:
            r = subprocess.run(
                ['npm', 'install', '--no-audit', '--no-fund'],
                cwd=app_dir, capture_output=True, text=True, timeout=300
            )
            if r.returncode == 0:
                installed.append('npm: package.json')
                log_event('SUCCESS', 'npm installed package.json')
            else:
                log_event('ERROR', f'npm failed: {r.stderr[-200:]}')
        except FileNotFoundError:
            log_event('WARN', 'npm not installed — skipping')
        except Exception as e:
            log_event('ERROR', f'npm error: {e}')

    return installed

def sync_app_statuses():
    conn = get_db()
    rows = conn.execute('SELECT * FROM apps WHERE status=?', ('running',)).fetchall()
    for r in rows:
        if not is_pid_alive(r['pid']):
            conn.execute('UPDATE apps SET status=?, pid=NULL WHERE id=?', ('stopped', r['id']))
    conn.commit()
    conn.close()

# ════════════════════ APP CONTROL ════════════════════
def start_app(app_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM apps WHERE id=?', (app_id,)).fetchone()
    if not row:
        conn.close()
        return False, 'App not found'
    if row['status'] == 'running' and is_pid_alive(row['pid']):
        conn.close()
        return False, 'Already running'

    app_dir = os.path.join(APPS_DIR, row['slug'])
    entry = row['entry']
    port = row['port']
    app_type = row['app_type'] or 'python'

    if port_in_use(port):
        log_event('WARN', f'Port {port} already in use')

    log_path = os.path.join(app_dir, 'app.log')
    log_file = open(log_path, 'a', encoding='utf-8')
    log_file.write(f"\n{'='*60}\n--- Started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} on port {port} ---\n{'='*60}\n")
    log_file.flush()

    env = os.environ.copy()
    env['PORT'] = str(port)
    env['PYTHONUNBUFFERED'] = '1'
    env['NODE_ENV'] = 'production'
    env['FLASK_DEBUG'] = '0'
    env['BASE_PATH'] = app_path(row['slug'])

    try:
        if app_type in ('python', 'flask'):
            entry_path = os.path.join(app_dir, entry)
            if not os.path.exists(entry_path):
                conn.close()
                return False, f'Entry missing: {entry}'
            cwd = app_dir
            if app_type == 'flask':
                if not ensure_flask():
                    conn.close()
                    return False, 'Flask not available (pip install failed)'
                mod = entry[:-3].replace(os.sep, '.').replace('/', '.')
                cmd = [sys.executable, '-u', '-m', 'flask', '--app', mod,
                       'run', '--host', '0.0.0.0', '--port', str(port)]
            else:
                cmd = [sys.executable, '-u', entry]

        elif app_type == 'node':
            cmd = ['node', entry]
            cwd = app_dir

        elif app_type == 'static':
            static_root = app_dir if entry == '.' else os.path.join(app_dir, entry)
            if not os.path.exists(os.path.join(static_root, 'index.html')):
                conn.close()
                return False, 'index.html not found'
            cmd = [sys.executable, '-m', 'http.server', str(port),
                   '--bind', '0.0.0.0', '--directory', static_root]
            cwd = app_dir
        else:
            conn.close()
            return False, f'Unknown type: {app_type}'

        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True
        )
        _running[app_id] = proc
        conn.execute('UPDATE apps SET status=?, pid=? WHERE id=?',
                     ('running', proc.pid, app_id))
        conn.commit()
        log_event('SUCCESS', f"Started '{row['name']}' (PID {proc.pid}, port {port})")
        return True, proc.pid
    except FileNotFoundError as e:
        log_event('ERROR', f"Runtime missing: {e}")
        return False, f'Runtime missing: {e}'
    except Exception as e:
        log_event('ERROR', f"Start failed: {e}")
        return False, str(e)
    finally:
        conn.close()

def stop_app(app_id, quiet=False):
    conn = get_db()
    row = conn.execute('SELECT * FROM apps WHERE id=?', (app_id,)).fetchone()
    if not row:
        conn.close()
        return False, 'App not found'

    stopped = False
    proc = _running.get(app_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            stopped = True
        except Exception as e:
            log_event('ERROR', f'Terminate failed: {e}')
    elif row['pid'] and is_pid_alive(row['pid']):
        try:
            os.kill(row['pid'], signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.1)
                if not is_pid_alive(row['pid']):
                    stopped = True
                    break
            if not stopped:
                os.kill(row['pid'], signal.SIGKILL)
                stopped = True
        except Exception as e:
            log_event('ERROR', f'Kill failed: {e}')

    _running.pop(app_id, None)
    conn.execute('UPDATE apps SET status=?, pid=NULL WHERE id=?', ('stopped', app_id))
    conn.commit()
    conn.close()
    if not quiet:
        log_event('INFO', f"Stopped '{row['name']}'")
    return True, 'stopped'

def cleanup():
    for app_id, proc in list(_running.items()):
        if proc.poll() is None:
            try: proc.terminate()
            except Exception: pass
    _running.clear()

atexit.register(cleanup)

# ════════════════════ REVERSE PROXY ════════════════════
def _rewrite_location(loc, slug):
    if not loc:
        return loc
    if loc.startswith('/') and not loc.startswith('/p/'):
        return app_path(slug) + loc.lstrip('/')
    return loc

def _rewrite_cookie(value, slug):
    if 'path=' not in value.lower():
        return value + f'; Path={app_path(slug)}'
    if '/p/' in value.lower():
        return value
    return re.sub(r'(?i)\bpath=/', f'path={app_path(slug)}', value)

def _rewrite_html(text, slug):
    prefix = app_path(slug)
    text = re.sub(r'(?i)(\b(?:href|src|action|poster|data-src)\s*=\s*["\'])/(?!/|p/)',
                  lambda m: m.group(1) + prefix, text)
    text = re.sub(r'(?i)(url\(\s*["\']?)/(?!/|p/)',
                  lambda m: m.group(1) + prefix, text)
    base = f'<base href="{prefix}">'
    if re.search(r'(?i)<head[^>]*>', text):
        text = re.sub(r'(?i)(<head[^>]*>)', lambda m: m.group(1) + base, text, count=1)
    else:
        text = base + text
    return text

def _error_page(code, title, detail):
    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{code} — {title}</title>
<style>body{{font-family:system-ui,sans-serif;background:#0a0a0f;color:#f0f0f5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;padding:24px}}
h1{{font-size:52px;margin:0 0 8px;background:linear-gradient(135deg,#8b5cf6,#06b6d4);
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
p{{color:#8b8b9e;font-size:13px;max-width:420px;line-height:1.6}}</style></head>
<body><div><h1>{code}</h1><p>{detail}</p></div></body></html>'''
    return Response(html, status=code, mimetype='text/html')

@app.route('/p/<slug>')
def proxy_redirect(slug):
    qs = request.query_string.decode()
    return redirect(app_path(slug) + (('?' + qs) if qs else ''))

@app.route('/p/<slug>/', defaults={'rest': ''}, methods=PROXY_METHODS)
@app.route('/p/<slug>/<path:rest>', methods=PROXY_METHODS)
def proxy_app(slug, rest):
    conn = get_db()
    row = conn.execute('SELECT * FROM apps WHERE slug=?', (slug,)).fetchone()
    conn.close()

    if not row:
        return _error_page(404, 'Not found', 'No hosted app with that name.')

    alive = row['status'] == 'running' and is_pid_alive(row['pid'])
    if not alive:
        return _error_page(502, 'Not running',
                           f'"{row["name"]}" is stopped. Start it from the console.')

    target = '/' + rest
    qs = request.query_string.decode()
    if qs:
        target += '?' + qs

    headers = {}
    for k, v in request.headers.items():
        if k.lower() in HOP_HEADERS:
            continue
        headers[k] = v
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Prefix'] = app_path(slug)

    body = request.get_data() if request.method not in ('GET', 'HEAD') else None
    if body:
        headers['Content-Length'] = str(len(body))

    upstream = None
    try:
        upstream = http.client.HTTPConnection('127.0.0.1', row['port'], timeout=90)
        upstream.request(request.method, target, body=body, headers=headers)
        resp = upstream.getresponse()
        data = resp.read()
        status = resp.status
        raw_headers = resp.getheaders()
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        return _error_page(502, 'Bad gateway', f'Upstream unreachable: {e}')
    except Exception as e:
        return _error_page(502, 'Proxy error', str(e))
    finally:
        if upstream:
            try: upstream.close()
            except Exception: pass

    ctype = ''
    out = []
    for k, v in raw_headers:
        lk = k.lower()
        if lk in ('transfer-encoding', 'connection', 'content-length',
                  'content-encoding', 'keep-alive'):
            continue
        if lk == 'content-type':
            ctype = v.lower()
        if lk == 'location':
            v = _rewrite_location(v, slug)
        if lk == 'set-cookie':
            v = _rewrite_cookie(v, slug)
        out.append((k, v))

    if 'text/html' in ctype:
        try:
            data = _rewrite_html(data.decode('utf-8', 'replace'), slug).encode('utf-8')
        except Exception:
            pass
    elif 'text/css' in ctype:
        try:
            prefix = app_path(slug)
            css = re.sub(r'(?i)(url\(\s*["\']?)/(?!/|p/)',
                         lambda m: m.group(1) + prefix, data.decode('utf-8', 'replace'))
            data = css.encode('utf-8')
        except Exception:
            pass

    r = Response(data, status=status)
    for k, v in out:
        r.headers.add(k, v)
    r.headers['Content-Length'] = str(len(data))
    return r

# ════════════════════ AUTH ROUTES ════════════════════
@app.route('/')
def index():
    if 'user' in session:
        return render_template_string(TEMPLATE, css=CSS, logged_in=True,
                                      username=session['user'])
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user'] = username
            log_event('SUCCESS', f"Login: {username}")
            return redirect(url_for('index'))
        log_event('WARN', f"Failed login: {username}")
        return render_template_string(TEMPLATE, css=CSS, logged_in=False,
                                      error='Invalid credentials', admin_user=ADMIN_USERNAME)
    if 'user' in session:
        return redirect(url_for('index'))
    return render_template_string(TEMPLATE, css=CSS, logged_in=False,
                                  error=None, admin_user=ADMIN_USERNAME)

@app.route('/logout')
def logout():
    if 'user' in session:
        log_event('INFO', f"Logout: {session['user']}")
    session.clear()
    return redirect(url_for('login'))

# ════════════════════ SYSTEM API ════════════════════
@app.route('/api/system')
@login_required
def system_info():
    conn = get_db()
    apps_count = conn.execute('SELECT COUNT(*) FROM apps').fetchone()[0]
    files_count = conn.execute('SELECT COUNT(*) FROM hosted_files').fetchone()[0]
    files_size = conn.execute('SELECT COALESCE(SUM(filesize),0) FROM hosted_files').fetchone()[0]
    conn.close()

    return jsonify({
        'hostname': socket.gethostname(),
        'os': os.uname().sysname if hasattr(os, 'uname') else 'Unknown',
        'python': sys.version.split()[0],
        'psutil': HAS_PSUTIL,
        'cpu': safe_cpu(),
        'memory': safe_memory(),
        'disk': safe_disk(),
        'uptime': safe_uptime(),
        'apps_count': apps_count,
        'files_count': files_count,
        'files_size': files_size,
        'external': bool(external_base()),
        'base': external_base(),
    })

# ════════════════════ APP APIs ════════════════════
@app.route('/api/apps')
@login_required
def list_apps():
    conn = get_db()
    rows = conn.execute('SELECT * FROM apps ORDER BY id DESC').fetchall()
    conn.close()
    host = request.host.split(':')[0]
    out = []
    for r in rows:
        d = dict(r)
        d['alive'] = d['status'] == 'running' and is_pid_alive(d['pid'])
        if d['status'] == 'running' and not d['alive']:
            d['status'] = 'stopped'
        d['url'] = app_url(r, host)
        d['path'] = app_path(r['slug'])
        out.append(d)
    return jsonify(out)

@app.route('/api/apps/analyze-zip', methods=['POST'])
@login_required
def analyze_zip():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Must be a .zip file'}), 400

    temp_id = secrets.token_hex(8)
    temp_path = os.path.join(TEMP_DIR, temp_id)
    os.makedirs(temp_path, exist_ok=True)

    try:
        zip_path = os.path.join(temp_path, '_upload.zip')
        f.save(zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                if member.startswith('/') or '..' in member:
                    shutil.rmtree(temp_path, ignore_errors=True)
                    return jsonify({'error': 'Unsafe zip archive'}), 400
            zf.extractall(temp_path)
        os.remove(zip_path)

        items = [i for i in os.listdir(temp_path) if not i.startswith('.')]
        if len(items) == 1 and os.path.isdir(os.path.join(temp_path, items[0])):
            inner = os.path.join(temp_path, items[0])
            for it in os.listdir(inner):
                shutil.move(os.path.join(inner, it), os.path.join(temp_path, it))
            os.rmdir(inner)

        scan = scan_zip_contents(temp_path)
        return jsonify({
            'temp_id': temp_id,
            'files': scan['files'],
            'entries': scan['entries'],
            'total_size': scan['total_size'],
            'file_count': scan['file_count']
        })
    except Exception as e:
        shutil.rmtree(temp_path, ignore_errors=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/confirm', methods=['POST'])
@login_required
def confirm_app():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    entry = (data.get('entry') or '').strip()
    app_type = (data.get('app_type') or '').strip()
    temp_id = (data.get('temp_id') or '').strip()

    if not name:
        return jsonify({'error': 'Name required'}), 400
    if not entry:
        return jsonify({'error': 'Entry file required'}), 400
    if app_type not in APP_TYPES:
        return jsonify({'error': 'Invalid app type'}), 400

    slug = secure_filename(name.lower().replace(' ', '-'))
    if not slug:
        return jsonify({'error': 'Invalid name'}), 400

    app_dir = os.path.join(APPS_DIR, slug)
    if os.path.exists(app_dir):
        return jsonify({'error': f"App '{slug}' already exists"}), 409

    if temp_id:
        temp_path = os.path.join(TEMP_DIR, temp_id)
        if not os.path.exists(temp_path):
            return jsonify({'error': 'Upload expired, re-upload zip'}), 400
        try:
            shutil.move(temp_path, app_dir)
        except Exception as e:
            shutil.rmtree(temp_path, ignore_errors=True)
            return jsonify({'error': f'Move failed: {e}'}), 500
    else:
        return jsonify({'error': 'No upload data — re-upload'}), 400

    installed = auto_install(app_dir, app_type)

    port = find_free_port()
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO apps (name, slug, entry, port, status, app_type) VALUES (?,?,?,?,?,?)',
        (name, slug, entry, port, 'stopped', app_type)
    )
    app_id = cur.lastrowid
    conn.commit()
    conn.close()

    log_event('SUCCESS', f"Created '{name}' ({app_type}) on port {port}")
    return jsonify({
        'id': app_id, 'slug': slug, 'port': port, 'path': app_path(slug),
        'entry': entry, 'type': app_type, 'installed': installed
    }), 201

@app.route('/api/apps/create-direct', methods=['POST'])
@login_required
def create_direct():
    name = (request.form.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'File required'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty file'}), 400

    filename = secure_filename(f.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext == 'zip':
        return jsonify({'error': 'Use analyze-zip endpoint for zip files'}), 400
    if ext not in ('py', 'js', 'html'):
        return jsonify({'error': 'Only .py, .js, .html accepted here'}), 400

    slug = secure_filename(name.lower().replace(' ', '-'))
    if not slug:
        return jsonify({'error': 'Invalid name'}), 400

    app_dir = os.path.join(APPS_DIR, slug)
    if os.path.exists(app_dir):
        return jsonify({'error': f"App '{slug}' exists"}), 409

    os.makedirs(app_dir, exist_ok=True)

    try:
        if ext == 'py':
            entry = 'main.py'
            f.save(os.path.join(app_dir, entry))
            app_type = 'flask' if looks_like_flask(os.path.join(app_dir, entry)) else 'python'
        elif ext == 'js':
            entry, app_type = 'server.js', 'node'
            f.save(os.path.join(app_dir, entry))
            with open(os.path.join(app_dir, 'package.json'), 'w') as pf:
                json.dump({'name': slug, 'main': entry,
                           'scripts': {'start': f'node {entry}'}}, pf)
        else:
            entry, app_type = '.', 'static'
            f.save(os.path.join(app_dir, 'index.html'))

        if 'requirements' in request.files:
            req = request.files['requirements']
            if req and req.filename:
                req.save(os.path.join(app_dir, 'requirements.txt'))

        installed = auto_install(app_dir, app_type)
        port = find_free_port()

        conn = get_db()
        cur = conn.execute(
            'INSERT INTO apps (name, slug, entry, port, status, app_type) VALUES (?,?,?,?,?,?)',
            (name, slug, entry, port, 'stopped', app_type)
        )
        app_id = cur.lastrowid
        conn.commit()
        conn.close()

        log_event('SUCCESS', f"Created '{name}' ({app_type}) on port {port}")
        return jsonify({
            'id': app_id, 'slug': slug, 'port': port, 'path': app_path(slug),
            'entry': entry, 'type': app_type, 'installed': installed
        }), 201
    except Exception as e:
        shutil.rmtree(app_dir, ignore_errors=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/sample', methods=['POST'])
@login_required
def create_sample():
    slug = 'sample-hello-' + secrets.token_hex(3)
    app_dir = os.path.join(APPS_DIR, slug)
    os.makedirs(app_dir, exist_ok=True)

    code = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import os
PORT = int(os.environ.get("PORT", "8000"))
BASE = os.environ.get("BASE_PATH", "/")
HTML = """<!DOCTYPE html><html><head><title>RAXX HOST</title>
<style>
body{font-family:system-ui,sans-serif;background:#0a0a0f;color:#f0f0f5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
h1{font-size:44px;background:linear-gradient(135deg,#8b5cf6,#06b6d4);
-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0 0 12px}
p{color:#8b8b9e;font-size:14px}.pill{display:inline-block;padding:5px 14px;
border-radius:20px;background:rgba(16,185,129,.15);color:#10b981;
font-size:12px;font-weight:700;margin-bottom:16px}
</style></head><body><div style="text-align:center">
<div class="pill">● RUNNING</div>
<h1>Hello from RAXX HOST</h1>
<p>Served through BASE_X on internal port PORT_X</p>
</div></body></html>"""
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = HTML.replace("PORT_X", str(PORT)).replace("BASE_X", BASE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *a):
        print(f"[{self.log_date_time_string()}] {fmt % a}")
if __name__ == "__main__":
    print(f"Sample on 0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
'''
    with open(os.path.join(app_dir, 'main.py'), 'w', encoding='utf-8') as f:
        f.write(code)

    port = find_free_port()
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO apps (name, slug, entry, port, status, app_type) VALUES (?,?,?,?,?,?)',
        ('Sample Hello', slug, 'main.py', port, 'stopped', 'python')
    )
    app_id = cur.lastrowid
    conn.commit()
    conn.close()
    log_event('SUCCESS', f"Created sample app on port {port}")
    return jsonify({'id': app_id, 'port': port, 'slug': slug, 'path': app_path(slug)}), 201

@app.route('/api/apps/<int:app_id>/start', methods=['POST'])
@login_required
def api_start(app_id):
    ok, result = start_app(app_id)
    if ok: return jsonify({'ok': True, 'pid': result})
    return jsonify({'error': result}), 400

@app.route('/api/apps/<int:app_id>/stop', methods=['POST'])
@login_required
def api_stop(app_id):
    ok, result = stop_app(app_id)
    if ok: return jsonify({'ok': True})
    return jsonify({'error': result}), 400

@app.route('/api/apps/<int:app_id>/restart', methods=['POST'])
@login_required
def api_restart(app_id):
    stop_app(app_id, quiet=True)
    time.sleep(0.4)
    ok, result = start_app(app_id)
    if ok: return jsonify({'ok': True, 'pid': result})
    return jsonify({'error': result}), 400

@app.route('/api/apps/<int:app_id>/endpoint', methods=['PUT'])
@login_required
def api_edit_endpoint(app_id):
    data = request.get_json() or {}

    conn = get_db()
    row = conn.execute('SELECT * FROM apps WHERE id=?', (app_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'App not found'}), 404

    new_port = row['port']
    if 'port' in data and str(data.get('port') or '').strip() != '':
        try:
            new_port = int(data.get('port'))
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'error': 'Invalid port'}), 400
        if new_port < 1024 or new_port > 65535:
            conn.close()
            return jsonify({'error': 'Port must be 1024-65535'}), 400

    new_url = row['public_url'] or ''
    if 'public_url' in data:
        new_url = normalize_url(data.get('public_url'))
        if len(new_url) > 300:
            conn.close()
            return jsonify({'error': 'URL too long'}), 400

    if new_port != row['port']:
        conflict = conn.execute('SELECT id, name FROM apps WHERE port=? AND id!=?',
                                (new_port, app_id)).fetchone()
        if conflict:
            conn.close()
            return jsonify({'error': f'Port {new_port} used by "{conflict["name"]}"'}), 409

    was_running = row['status'] == 'running' and is_pid_alive(row['pid'])
    changed_port = new_port != row['port']
    conn.close()

    if was_running and changed_port:
        stop_app(app_id, quiet=True)
        time.sleep(0.3)

    conn = get_db()
    conn.execute('UPDATE apps SET port=?, public_url=? WHERE id=?',
                 (new_port, new_url, app_id))
    conn.commit()
    conn.close()

    log_event('INFO', f"Endpoint updated for '{row['name']}': port {new_port}, url '{new_url or 'auto'}'")

    restarted = False
    if was_running and changed_port:
        ok, _ = start_app(app_id)
        restarted = ok

    conn = get_db()
    fresh = conn.execute('SELECT * FROM apps WHERE id=?', (app_id,)).fetchone()
    conn.close()
    return jsonify({
        'ok': True,
        'port': new_port,
        'public_url': new_url,
        'path': app_path(fresh['slug']),
        'url': app_url(fresh, request.host.split(':')[0]),
        'restarted': restarted
    })

@app.route('/api/apps/<int:app_id>/port', methods=['PUT'])
@login_required
def api_edit_port(app_id):
    return api_edit_endpoint(app_id)

@app.route('/api/apps/<int:app_id>', methods=['DELETE'])
@login_required
def api_delete(app_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM apps WHERE id=?', (app_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'App not found'}), 404
    stop_app(app_id, quiet=True)
    shutil.rmtree(os.path.join(APPS_DIR, row['slug']), ignore_errors=True)
    conn.execute('DELETE FROM apps WHERE id=?', (app_id,))
    conn.commit()
    conn.close()
    log_event('WARN', f"Deleted '{row['name']}'")
    return jsonify({'ok': True})

@app.route('/api/apps/<int:app_id>/logs')
@login_required
def api_app_logs(app_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM apps WHERE id=?', (app_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'App not found'}), 404

    log_path = os.path.join(APPS_DIR, row['slug'], 'app.log')
    if not os.path.exists(log_path):
        return jsonify({'logs': 'No logs yet — start the app.'})
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    lines = content.splitlines()
    return jsonify({'logs': '\n'.join(lines[-500:]) or 'No logs yet.'})

# ════════════════════ FILE HOSTING APIs ════════════════════
@app.route('/api/files')
@login_required
def list_files():
    conn = get_db()
    rows = conn.execute('SELECT * FROM hosted_files ORDER BY id DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/files/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    original = secure_filename(f.filename)
    ext = original.rsplit('.', 1)[-1].upper() if '.' in original else 'BIN'
    stored = f"{int(time.time())}_{secrets.token_hex(4)}_{original}"

    path = os.path.join(FILES_DIR, stored)
    f.save(path)
    size = os.path.getsize(path)

    conn = get_db()
    cur = conn.execute(
        'INSERT INTO hosted_files (filename, stored_name, filesize, filetype) VALUES (?,?,?,?)',
        (original, stored, size, ext))
    conn.commit()
    row = conn.execute('SELECT * FROM hosted_files WHERE id=?', (cur.lastrowid,)).fetchone()
    conn.close()
    log_event('SUCCESS', f"Hosted file: {original} ({human_size(size)})")
    return jsonify(dict(row)), 201

@app.route('/api/files/<int:file_id>', methods=['DELETE'])
@login_required
def delete_hosted_file(file_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM hosted_files WHERE id=?', (file_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    path = os.path.join(FILES_DIR, row['stored_name'])
    if os.path.exists(path):
        os.remove(path)
    conn.execute('DELETE FROM hosted_files WHERE id=?', (file_id,))
    conn.commit()
    conn.close()
    log_event('WARN', f"Deleted hosted file: {row['filename']}")
    return jsonify({'ok': True})

# Public — shareable links without login
@app.route('/files/<int:file_id>')
def serve_file(file_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM hosted_files WHERE id=?', (file_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    path = os.path.join(FILES_DIR, row['stored_name'])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, download_name=row['filename'], as_attachment=False)

@app.route('/files/<int:file_id>/download')
def download_file(file_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM hosted_files WHERE id=?', (file_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    path = os.path.join(FILES_DIR, row['stored_name'])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, download_name=row['filename'], as_attachment=True)

# ════════════════════ LOGS API ════════════════════
@app.route('/api/logs')
@login_required
def api_logs():
    level = request.args.get('level', 'ALL')
    limit = min(int(request.args.get('limit', 200)), 1000)
    conn = get_db()
    if level and level != 'ALL':
        rows = conn.execute('SELECT * FROM logs WHERE level=? ORDER BY id DESC LIMIT ?',
                            (level, limit)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/logs', methods=['DELETE'])
@login_required
def api_clear_logs():
    conn = get_db()
    conn.execute('DELETE FROM logs')
    conn.commit()
    conn.close()
    log_event('INFO', 'Logs cleared')
    return jsonify({'ok': True})

@app.route('/api/password', methods=['POST'])
@login_required
def api_password():
    data = request.get_json() or {}
    old = data.get('old', '')
    new = data.get('new', '')
    if len(new) < 4:
        return jsonify({'error': 'New password too short (min 4)'}), 400
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE username=?', (session['user'],)).fetchone()
    if not user or not check_password_hash(user['password_hash'], old):
        conn.close()
        return jsonify({'error': 'Current password incorrect'}), 401
    conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                 (generate_password_hash(new), user['id']))
    conn.commit()
    conn.close()
    log_event('SUCCESS', f"Password changed for {session['user']}")
    return jsonify({'ok': True})

# ════════════════════ CSS ════════════════════
CSS = r'''
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
  background:#0a0a0f;color:#f0f0f5;font-size:14px;line-height:1.5;
  -webkit-font-smoothing:antialiased;-webkit-tap-highlight-color:transparent;
  transition:background .3s,color .3s;
}
[data-theme="light"] body{background:#f5f5fa;color:#1a1a24}
a{color:inherit;text-decoration:none}
button,input,textarea,select{font-family:inherit;font-size:inherit}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2a2a35;border-radius:10px}
::-webkit-scrollbar-thumb:hover{background:#3a3a48}
[data-theme="light"] ::-webkit-scrollbar-thumb{background:#c5c5d5}

/* ═══ LIGHT THEME OVERRIDES ═══ */
[data-theme="light"] .login-screen{background:radial-gradient(circle at 50% 0%,#e0d5ff 0%,#f5f5fa 55%)}
[data-theme="light"] .login-card{background:rgba(255,255,255,0.92);border-color:#e0e0ea}
[data-theme="light"] .login-sub{color:#6a6a7a}
[data-theme="light"] .form-group label{color:#4a4a5a}
[data-theme="light"] .form-group input{background:#fff;border-color:#d5d5e0;color:#1a1a24}
[data-theme="light"] .form-group input:focus{border-color:#8b5cf6}
[data-theme="light"] .orbit-svg text{fill:#6a6a7a}
[data-theme="light"] .sidebar{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .brand-text b{color:#1a1a24}
[data-theme="light"] .brand-text span{color:#8a8a9a}
[data-theme="light"] .nav-item{color:#6a6a7a}
[data-theme="light"] .nav-item:hover{background:#f0f0f8;color:#1a1a24}
[data-theme="light"] .nav-item.active{background:rgba(139,92,246,0.1);color:#7c3aed}
[data-theme="light"] .sidebar-user{background:#f0f0f8;border-color:#e0e0ea}
[data-theme="light"] .main{background:#f5f5fa}
[data-theme="light"] .topbar h1{color:#1a1a24}
[data-theme="light"] .topbar-sub{color:#6a6a7a}
[data-theme="light"] .card{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .hero{background:linear-gradient(135deg,rgba(139,92,246,.1),rgba(6,182,212,.05));border-color:#e0e0ea}
[data-theme="light"] .hero p{color:#5a5a6a}
[data-theme="light"] .hero-ring svg text{fill:#8a8a9a}
[data-theme="light"] .tb-orbit svg text{fill:#8a8a9a}
[data-theme="light"] .stat{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .stat-label{color:#6a6a7a}
[data-theme="light"] .app-card{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .app-card:hover{border-color:#c9b8f5}
[data-theme="light"] .app-title h3{color:#1a1a24}
[data-theme="light"] .app-title .meta{color:#6a6a7a}
[data-theme="light"] .app-footer{border-color:#e8e8f0}
[data-theme="light"] .app-footer strong{color:#4a4a5a}
[data-theme="light"] .input-text{background:#f8f8fc;border-color:#d5d5e0;color:#1a1a24}
[data-theme="light"] .file-pick{background:#f8f8fc;border-color:#d5d5e0}
[data-theme="light"] .file-pick:hover{border-color:#8b5cf6;background:rgba(139,92,246,.05)}
[data-theme="light"] .fp-icon{background:#fff;border-color:#e0e0ea;color:#7c3aed}
[data-theme="light"] .fp-text b{color:#1a1a24}
[data-theme="light"] .fp-text span{color:#7a7a8a}
[data-theme="light"] .btn-secondary{background:#f0f0f8;color:#1a1a24;border-color:#d5d5e0}
[data-theme="light"] .btn-secondary:hover:not(:disabled){background:#e8e8f2;border-color:#c5c5d5}
[data-theme="light"] .filter-chip{background:#fff;border-color:#e0e0ea;color:#6a6a7a}
[data-theme="light"] .filter-chip:hover{background:#f0f0f8;color:#1a1a24}
[data-theme="light"] .filter-chip.active{background:rgba(139,92,246,0.1);border-color:#8b5cf6;color:#7c3aed}
[data-theme="light"] .modal{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .modal-header{border-color:#e8e8f0}
[data-theme="light"] .modal-footer{border-color:#e8e8f0}
[data-theme="light"] .modal-body pre{background:#f8f8fc;color:#1a1a24}
[data-theme="light"] .terminal{background:#f8f8fc;border-color:#e0e0ea}
[data-theme="light"] .terminal .lt{color:#8a8a9a}
[data-theme="light"] .terminal .lm{color:#1a1a24}
[data-theme="light"] .menu-btn{background:#fff;border-color:#e0e0ea;color:#1a1a24}
[data-theme="light"] .theme-btn{background:#fff;border-color:#e0e0ea;color:#1a1a24}
[data-theme="light"] .toast{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .file-row{background:#fff;border-color:#e0e0ea}
[data-theme="light"] .sys-item{background:#f8f8fc;border-color:#e0e0ea}
[data-theme="light"] .zip-file-item:hover{background:#f0f0f8}
[data-theme="light"] .zip-file-item.selected{background:rgba(139,92,246,0.1);border-color:#8b5cf6}
[data-theme="light"] .zip-file-list{border-color:#e0e0ea}
[data-theme="light"] .zip-summary{background:#f8f8fc;border-color:#e0e0ea}
[data-theme="light"] .endpoint-preview{background:#f8f8fc;border-color:#e0e0ea}
[data-theme="light"] .path-box{background:#f8f8fc;border-color:#e0e0ea}

/* ═══ LOGIN ═══ */
.login-screen{
  min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;
  padding:24px 20px;background:radial-gradient(circle at 50% 0%,#1a1230 0%,#0a0a0f 55%);
  transition:background .3s;
}
.login-card{
  width:100%;max-width:420px;background:rgba(22,22,31,0.85);
  backdrop-filter:blur(20px);border:1px solid #26262f;border-radius:20px;
  padding:0 32px 32px;box-shadow:0 30px 80px -20px rgba(0,0,0,0.8),0 0 0 1px rgba(139,92,246,0.06);
  position:relative;overflow:hidden;transition:background .3s;
}
.login-card::before{
  content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;
  background:radial-gradient(circle at 50% 50%,rgba(139,92,246,0.08),transparent 40%);
  animation:rotate 20s linear infinite;pointer-events:none;
}
@keyframes rotate{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}

.orbit-wrap{
  position:relative;width:270px;height:270px;margin:20px auto 0;
  display:flex;align-items:center;justify-content:center;
}
.orbit-glow{
  position:absolute;inset:18%;border-radius:50%;
  background:radial-gradient(circle,rgba(139,92,246,0.35),transparent 65%);
  filter:blur(30px);animation:pulse 3.5s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:.5;transform:scale(1)}50%{opacity:1;transform:scale(1.1)}}
.orbit-svg{position:absolute;inset:0;width:100%;height:100%;animation:spin 30s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.orbit-svg text{fill:#8b8b9e;font-size:13px;letter-spacing:5px;font-weight:600;text-transform:uppercase}
.logo-core{
  position:relative;width:88px;height:88px;border-radius:24px;
  background:linear-gradient(135deg,#8b5cf6 0%,#06b6d4 100%);
  display:flex;align-items:center;justify-content:center;
  font-size:42px;font-weight:900;color:#fff;
  box-shadow:0 0 60px rgba(139,92,246,0.6),inset 0 0 24px rgba(255,255,255,0.2);
  animation:float 5s ease-in-out infinite;
}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}

.login-card h1{
  font-size:26px;font-weight:900;text-align:center;letter-spacing:-0.5px;margin-bottom:4px;
  background:linear-gradient(135deg,#c4b5fd,#67e8f9);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.login-sub{color:#8b8b9e;text-align:center;font-size:13px;margin-bottom:24px}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:12px;font-weight:600;color:#a0a0b0;margin-bottom:6px}
.form-group input{
  width:100%;padding:13px 15px;background:#0a0a0f;border:1.5px solid #26262f;
  border-radius:10px;color:#f0f0f5;outline:none;transition:.15s;font-size:14px;
}
.form-group input:focus{border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,0.15)}
.login-error{
  padding:11px 15px;background:rgba(244,63,94,0.1);
  border:1px solid rgba(244,63,94,0.3);border-radius:10px;
  color:#f43f5e;font-size:12.5px;font-weight:600;margin-bottom:14px;
}
.login-hint{text-align:center;color:#8b8b9e;font-size:12px;margin-top:18px}
.login-hint b{color:#a5a6f6;font-family:monospace}

/* ═══ BUTTONS ═══ */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:10px 16px;border-radius:9px;font-size:13px;font-weight:600;
  border:1px solid transparent;cursor:pointer;transition:.15s;
  white-space:nowrap;min-height:38px;
}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn-primary{background:#8b5cf6;color:#fff}
.btn-primary:hover:not(:disabled){background:#7c3aed;transform:translateY(-1px);box-shadow:0 4px 14px -3px rgba(139,92,246,0.5)}
.btn-secondary{background:#1c1c26;color:#e4e4ef;border-color:#2a2a35}
.btn-secondary:hover:not(:disabled){background:#252533;border-color:#38384a}
.btn-success{background:#059669;color:#fff}
.btn-success:hover:not(:disabled){background:#047857}
.btn-danger{background:transparent;color:#f43f5e;border-color:#3a1f26}
.btn-danger:hover:not(:disabled){background:rgba(244,63,94,0.1);border-color:#f43f5e}
.btn-sm{padding:7px 12px;font-size:12px;min-height:32px}
.btn-block{width:100%}

/* ═══ LAYOUT ═══ */
#app{display:flex;min-height:100vh}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:99;backdrop-filter:blur(2px)}
.overlay.show{display:block}

.sidebar{
  width:250px;background:#0f0f16;border-right:1px solid #1e1e28;
  padding:20px 12px;display:flex;flex-direction:column;
  position:fixed;top:0;left:0;bottom:0;overflow-y:auto;z-index:100;
  transition:transform .25s ease,background .3s;
}
.sidebar-brand{
  display:flex;align-items:center;gap:11px;padding:6px 12px 22px;
}
.brand-orbit{
  position:relative;width:38px;height:38px;
  display:flex;align-items:center;justify-content:center;
}
.brand-orbit svg{
  position:absolute;inset:0;width:100%;height:100%;
  animation:spin 20s linear infinite;
}
.brand-orbit svg text{fill:#8b8b9e;font-size:26px;letter-spacing:1.5px;font-weight:700}
.brand-mark{
  position:relative;z-index:2;width:24px;height:24px;border-radius:7px;
  background:linear-gradient(135deg,#8b5cf6 0%,#06b6d4 100%);
  display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:900;color:#fff;
  box-shadow:0 3px 12px -2px rgba(139,92,246,0.6);
}
.brand-text b{font-size:14.5px;font-weight:800;display:block;line-height:1.15;letter-spacing:.2px}
.brand-text span{font-size:9px;color:#8b8b9e;letter-spacing:2px;font-weight:700}

.nav{display:flex;flex-direction:column;gap:2px;flex:1}
.nav-item{
  display:flex;align-items:center;gap:11px;padding:11px 13px;border-radius:9px;
  color:#a0a0b0;font-size:13.5px;font-weight:500;transition:.15s;
  background:none;border:none;cursor:pointer;text-align:left;width:100%;
}
.nav-item:hover{background:#1a1a24;color:#f0f0f5}
.nav-item.active{background:rgba(139,92,246,0.12);color:#a5a6f6}
.nav-item .icon{font-size:16px;width:20px;text-align:center}

.sidebar-user{
  margin-top:14px;padding:11px 12px;border-radius:11px;
  background:#14141d;border:1px solid #23232d;
  display:flex;align-items:center;gap:10px;transition:background .3s;
}
.user-avatar{
  width:34px;height:34px;border-radius:50%;
  background:linear-gradient(135deg,#8b5cf6 0%,#06b6d4 100%);
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:700;font-size:12px;flex-shrink:0;
}
.user-info{flex:1;min-width:0}
.user-info b{display:block;font-size:12.5px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.user-info span{font-size:10.5px;color:#8b8b9e}

.main{flex:1;margin-left:250px;padding:28px 36px 60px;min-height:100vh;max-width:1400px;transition:background .3s}

/* ═══ TOPBAR ═══ */
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px;gap:14px}
.topbar-left{display:flex;align-items:center;gap:12px;min-width:0}
.topbar h1{font-size:22px;font-weight:700;letter-spacing:-0.4px;margin:0}
.topbar-sub{color:#8b8b9e;font-size:12.5px;margin-top:3px}
.topbar-right{display:flex;align-items:center;gap:8px}
.tb-orbit{
  position:relative;width:44px;height:44px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
}
.tb-orbit svg{position:absolute;inset:0;width:100%;height:100%;animation:spin 22s linear infinite}
.tb-orbit svg text{fill:#8b8b9e;font-size:6.4px;letter-spacing:1.6px;font-weight:700;text-transform:uppercase}
.tb-core{
  width:26px;height:26px;border-radius:8px;z-index:2;
  background:linear-gradient(135deg,#8b5cf6,#06b6d4);
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:900;font-size:13px;
  box-shadow:0 4px 16px -3px rgba(139,92,246,.7);
}
.menu-btn{
  display:none;background:#1c1c26;border:1px solid #2a2a35;
  border-radius:9px;padding:8px 12px;color:#f0f0f5;cursor:pointer;
  font-size:18px;line-height:1;
}
.theme-btn{
  width:38px;height:38px;border-radius:9px;background:#1c1c26;
  border:1px solid #2a2a35;color:#e4e4ef;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  font-size:16px;transition:.15s;
}
.theme-btn:hover{background:#252533;border-color:#38384a}

/* ═══ PAGES ═══ */
.page{display:none}
.page.active{display:block;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ═══ HERO — orbit typography ═══ */
.hero{
  position:relative;overflow:hidden;border-radius:18px;padding:22px;margin-bottom:18px;
  background:linear-gradient(135deg,rgba(139,92,246,.13),rgba(6,182,212,.05));
  border:1px solid #23232d;display:flex;align-items:center;gap:24px;
}
.hero-ring{
  position:relative;width:230px;height:230px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
}
.hero-ring::after{
  content:'';position:absolute;inset:16%;border-radius:50%;
  background:radial-gradient(circle,rgba(139,92,246,.22),transparent 68%);
  filter:blur(22px);animation:pulse 4s ease-in-out infinite;
}
.hero-ring svg{position:absolute;inset:0;width:100%;height:100%;animation:spin 36s linear infinite}
.hero-ring svg text{
  fill:#8b8b9e;font-size:10.5px;letter-spacing:4.6px;font-weight:800;text-transform:uppercase;
}
.hero-center{position:relative;z-index:2;text-align:center}
.hero-center h2{
  font-size:23px;font-weight:900;line-height:1.02;letter-spacing:-.6px;
  background:linear-gradient(135deg,#c4b5fd,#67e8f9);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.hero-body{min-width:0}
.hero h3{font-size:14px;font-weight:800;letter-spacing:-.2px;margin-bottom:6px}
.hero p{font-size:12.5px;color:#8b8b9e;line-height:1.65;max-width:520px}

/* ═══ CARDS ═══ */
.card{background:#14141d;border:1px solid #23232d;border-radius:13px;padding:20px;margin-bottom:16px;transition:background .3s,border-color .3s}
.card-title{font-size:14px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.card-title .right{font-size:12px;color:#8b8b9e;font-weight:500}

/* ═══ STATS ═══ */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:18px}
.stat{background:#14141d;border:1px solid #23232d;border-radius:13px;padding:18px;transition:background .3s,border-color .3s}
.stat-label{font-size:11px;color:#8b8b9e;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.stat-value{font-size:26px;font-weight:800;letter-spacing:-0.5px;line-height:1.1}
.stat-value small{font-size:13px;color:#8b8b9e;font-weight:500;margin-left:3px}
.stat-bar{height:4px;background:#1e1e28;border-radius:4px;margin-top:12px;overflow:hidden}
.stat-bar-fill{height:100%;background:linear-gradient(90deg,#8b5cf6,#06b6d4);border-radius:4px;transition:width .4s}

/* ═══ FORM ═══ */
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.form-label{display:block;font-size:12px;color:#a0a0b0;font-weight:600;margin-bottom:6px}
.input-text{width:100%;padding:11px 13px;background:#0a0a0f;border:1.5px solid #26262f;border-radius:9px;color:#f0f0f5;outline:none;font-size:13px;transition:.15s}
.input-text:focus{border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,0.15)}

/* ═══ CUSTOM FILE PICKER ═══ */
.file-pick{
  display:flex;align-items:center;gap:11px;padding:11px 13px;
  border:1.5px dashed #2a2a35;border-radius:10px;background:#0a0a0f;
  cursor:pointer;transition:.15s;user-select:none;
}
.file-pick:hover{border-color:#8b5cf6;background:rgba(139,92,246,.06)}
.file-pick.drag{border-color:#8b5cf6;border-style:solid;background:rgba(139,92,246,.1)}
.file-pick.has-file{border-style:solid;border-color:rgba(16,185,129,.45);background:rgba(16,185,129,.07)}
.fp-icon{
  width:32px;height:32px;border-radius:9px;background:#15151f;border:1px solid #26262f;
  display:flex;align-items:center;justify-content:center;font-size:14px;
  flex-shrink:0;color:#a5a6f6;
}
.file-pick.has-file .fp-icon{color:#10b981;border-color:rgba(16,185,129,.35);background:rgba(16,185,129,.1)}
.fp-text{min-width:0;flex:1}
.fp-text b{display:block;font-size:12.5px;font-weight:700}
.fp-text span{display:block;font-size:11px;color:#8b8b9e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ═══ APP LIST ═══ */
.app-list{display:flex;flex-direction:column;gap:12px}
.app-card{background:#14141d;border:1px solid #23232d;border-radius:13px;padding:18px;transition:.15s}
.app-card:hover{border-color:#332b4d;box-shadow:0 8px 26px -18px rgba(139,92,246,.9)}
.app-header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px;flex-wrap:wrap}
.app-title{display:flex;align-items:center;gap:12px;min-width:0;flex:1}
.app-icon{
  width:42px;height:42px;border-radius:11px;
  background:linear-gradient(135deg,rgba(139,92,246,0.15),rgba(6,182,212,0.1));
  border:1px solid rgba(139,92,246,0.2);
  display:flex;align-items:center;justify-content:center;font-size:19px;flex-shrink:0;
}
.app-title h3{font-size:15px;font-weight:700;margin:0 0 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.app-title .meta{font-size:11.5px;color:#8b8b9e;font-family:'SF Mono','Monaco',monospace}
.app-actions{display:flex;gap:6px;flex-wrap:wrap}
.app-footer{display:flex;gap:12px;padding-top:13px;border-top:1px solid #1e1e28;font-size:11.5px;color:#8b8b9e;flex-wrap:wrap;align-items:center}
.app-footer strong{color:#a0a0b0;font-weight:600}

/* ═══ BADGES ═══ */
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.2px}
.badge .dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.running{background:rgba(16,185,129,0.12);color:#10b981}
.badge.stopped{background:rgba(139,139,158,0.15);color:#8b8b9e}
.badge.type{font-family:'SF Mono','Monaco',monospace;font-size:10.5px}
.badge.type.python{background:rgba(59,130,246,0.14);color:#60a5fa}
.badge.type.node{background:rgba(16,185,129,0.14);color:#34d399}
.badge.type.static{background:rgba(245,158,11,0.14);color:#fbbf24}
.badge.type.flask{background:rgba(168,85,247,0.16);color:#c084fc}
.badge.port{
  background:rgba(139,92,246,0.12);color:#a5a6f6;max-width:300px;
  font-family:'SF Mono','Monaco',monospace;cursor:pointer;border:none;transition:.15s;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.badge.port:hover{background:rgba(139,92,246,0.22);transform:translateY(-1px)}

/* ═══ FILE HOSTING ═══ */
.dropzone{
  border:2px dashed #2a2a35;border-radius:13px;padding:36px 20px;
  text-align:center;transition:.2s;cursor:pointer;background:#14141d;margin-bottom:16px;
}
.dropzone:hover,.dropzone.drag{border-color:#8b5cf6;background:rgba(139,92,246,0.06);transform:translateY(-2px)}
.dz-icon{font-size:36px;margin-bottom:10px;display:block}
.dropzone p{font-weight:700;font-size:14px;margin-bottom:4px}
.dropzone span{font-size:11.5px;color:#8b8b9e}

.file-row{
  display:flex;align-items:center;gap:14px;padding:13px 16px;border-radius:11px;
  background:#14141d;border:1px solid #23232d;margin-bottom:9px;transition:.15s;
}
.file-row:hover{border-color:#2e2e3a;transform:translateX(2px)}
.file-icon{
  width:40px;height:40px;border-radius:10px;
  background:linear-gradient(135deg,rgba(139,92,246,0.15),rgba(6,182,212,0.1));
  display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;
}
.file-info{flex:1;min-width:0}
.file-info b{font-size:13px;font-weight:700;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-info span{font-size:11px;color:#8b8b9e}
.file-actions{display:flex;gap:6px}

/* ═══ LOGS ═══ */
.log-toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.filter-chip{
  padding:8px 15px;border-radius:20px;background:#14141d;
  border:1px solid #23232d;color:#a0a0b0;font-size:12px;font-weight:700;
  cursor:pointer;transition:.15s;
}
.filter-chip:hover{background:#1a1a24;color:#f0f0f5}
.filter-chip.active{background:rgba(139,92,246,0.12);border-color:#8b5cf6;color:#a5a6f6}
.terminal{
  background:#050509;border:1px solid #1e1e28;border-radius:11px;padding:16px;
  font-family:'SF Mono','Monaco','Consolas',monospace;font-size:12px;line-height:1.75;
  height:calc(100vh - 320px);min-height:340px;overflow-y:auto;
}
.terminal .log-row{display:flex;gap:12px;padding:1px 0}
.terminal .lt{color:#4a4a66;flex-shrink:0;min-width:64px}
.terminal .ll{flex-shrink:0;font-weight:700;width:64px}
.terminal .lm{color:#c5c5d5;word-break:break-word}
.ll.INFO{color:#38bdf8}
.ll.SUCCESS{color:#10b981}
.ll.WARN{color:#f59e0b}
.ll.ERROR{color:#f43f5e}

/* ═══ MODAL ═══ */
.modal-backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:1000;
  display:none;align-items:center;justify-content:center;padding:16px;
  backdrop-filter:blur(4px);
}
.modal-backdrop.show{display:flex}
.modal{
  background:#14141d;border:1px solid #23232d;border-radius:14px;
  width:100%;max-width:820px;max-height:88vh;
  display:flex;flex-direction:column;
  box-shadow:0 25px 80px rgba(0,0,0,0.85);
}
.modal-md{max-width:540px}
.modal-lg{max-width:920px}
.modal-header{padding:18px 22px;border-bottom:1px solid #23232d;display:flex;align-items:center;justify-content:space-between;gap:12px}
.modal-header h3{font-size:15px;font-weight:700;margin:0}
.modal-body{padding:18px 22px;flex:1;overflow:auto}
.modal-body pre{
  margin:0;padding:16px 20px;font-family:'SF Mono','Monaco',monospace;
  font-size:12px;line-height:1.6;background:#050509;color:#c5c5d5;
  white-space:pre-wrap;word-break:break-word;min-height:300px;border-radius:10px;
}
.modal-footer{padding:14px 22px;border-top:1px solid #23232d;display:flex;justify-content:flex-end;gap:8px}

/* ZIP file picker */
.zip-summary{
  background:#0a0a0f;border:1px solid #23232d;border-radius:10px;
  padding:12px 14px;margin-bottom:14px;font-size:12px;color:#a0a0b0;
}
.zip-summary strong{color:#f0f0f5}
.zip-section-title{
  font-size:11.5px;font-weight:700;letter-spacing:0.7px;
  text-transform:uppercase;color:#8b8b9e;margin:14px 0 8px;
}
.zip-file-list{max-height:300px;overflow-y:auto;border:1px solid #23232d;border-radius:10px}
.zip-file-item{
  display:flex;align-items:center;gap:10px;padding:10px 14px;
  border-bottom:1px solid #1e1e28;cursor:pointer;transition:.15s;
}
.zip-file-item:last-child{border-bottom:none}
.zip-file-item:hover{background:#1a1a24}
.zip-file-item.selected{background:rgba(139,92,246,0.12);border-left:3px solid #8b5cf6}
.zip-file-item .kind-icon{font-size:15px;width:22px;text-align:center}
.zip-file-item .fpath{flex:1;font-family:'SF Mono','Monaco',monospace;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.zip-file-item .fsize{font-size:11px;color:#8b8b9e;font-family:monospace}
.zip-file-item .run-tag{
  font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;
  background:rgba(16,185,129,0.14);color:#34d399;
}

/* ═══ ENDPOINT EDITOR ═══ */
.path-box{
  display:flex;align-items:center;gap:8px;padding:11px 13px;margin-bottom:16px;
  background:#0a0a0f;border:1px dashed #332b4d;border-radius:10px;
  font-family:'SF Mono','Monaco',monospace;font-size:11.5px;color:#c084fc;
  word-break:break-all;
}
.endpoint-preview{
  display:flex;align-items:center;gap:10px;padding:11px 13px;margin-top:12px;
  background:#0a0a0f;border:1px solid #26262f;border-radius:10px;
  font-family:'SF Mono','Monaco',monospace;font-size:11.5px;color:#a5a6f6;
  word-break:break-all;
}
.endpoint-preview .ep-label{font-size:10px;color:#8b8b9e;font-family:inherit;letter-spacing:.6px;font-weight:700;text-transform:uppercase;flex-shrink:0}
.ep-actions{display:flex;gap:6px;margin-top:10px}

/* ═══ EMPTY ═══ */
.empty{padding:52px 24px;text-align:center;color:#8b8b9e}
.empty-icon{font-size:44px;margin-bottom:14px;opacity:0.5}
.empty h3{font-size:15px;color:#a0a0b0;font-weight:700;margin-bottom:6px}
.empty p{font-size:12.5px;margin-bottom:16px}

/* ═══ TOAST ═══ */
.toast-wrap{position:fixed;bottom:22px;right:22px;z-index:2000;display:flex;flex-direction:column;gap:8px;max-width:calc(100vw - 44px)}
.toast{
  padding:13px 18px;border-radius:10px;background:#14141d;
  border:1px solid #23232d;border-left:3px solid #8b5cf6;
  box-shadow:0 8px 24px rgba(0,0,0,0.55);font-size:13px;font-weight:500;
  animation:slideIn .25s ease;
}
.toast.ok{border-left-color:#10b981}
.toast.err{border-left-color:#f43f5e}
.toast.info{border-left-color:#38bdf8}
@keyframes slideIn{from{transform:translateX(120%);opacity:0}to{transform:none;opacity:1}}
.toast.removing{animation:slideOut .25s ease forwards}
@keyframes slideOut{to{transform:translateX(120%);opacity:0}}

/* ═══ MOBILE ═══ */
@media(max-width:900px){
  .sidebar{transform:translateX(-100%)}
  .sidebar.open{transform:translateX(0)}
  .main{margin-left:0;padding:18px 16px 40px}
  .menu-btn{display:flex}
  .tb-orbit{width:38px;height:38px}
  .topbar h1{font-size:18px}
  .topbar-sub{font-size:11.5px}
  .form-row{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr);gap:10px}
  .stat{padding:14px}
  .stat-value{font-size:22px}
  .card{padding:16px}
  .hero{padding:16px;gap:16px}
  .hero-ring{width:150px;height:150px}
  .hero-ring svg text{font-size:8px;letter-spacing:3px}
  .hero-center h2{font-size:17px}
  .hero p{font-size:11.5px}
  .app-header{flex-direction:column;align-items:flex-start}
  .app-actions{width:100%}
  .app-actions .btn{flex:1;justify-content:center}
  .terminal{height:calc(100vh - 280px);min-height:280px;font-size:11.5px;padding:12px}
  .login-card{padding:0 24px 24px}
  .orbit-wrap{width:220px;height:220px}
  .logo-core{width:76px;height:76px;font-size:36px}
}

@media(max-width:520px){
  .stats{grid-template-columns:1fr}
  .main{padding:14px 12px 40px}
  .card{padding:14px}
  .modal{max-height:92vh}
  .app-card{padding:14px}
  .app-title h3{font-size:14px}
  .file-row{padding:11px 12px;gap:10px}
  .file-icon{width:36px;height:36px;font-size:16px}
  .hero{flex-direction:column;align-items:center;text-align:center}
  .badge.port{max-width:100%}
}
'''

# ════════════════════ TEMPLATE ════════════════════
TEMPLATE = r'''
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>RAXX HOST</title>
<style>{{ css|safe }}</style>
</head>
<body>

{% if not logged_in %}
<div class="login-screen">
  <div class="login-card">
    <div class="orbit-wrap">
      <div class="orbit-glow"></div>
      <svg class="orbit-svg" viewBox="0 0 300 300">
        <defs>
          <path id="orbitPath" d="M 150,150 m -115,0 a 115,115 0 1,1 230,0 a 115,115 0 1,1 -230,0"/>
        </defs>
        <text>
          <textPath href="#orbitPath" startOffset="0">
            RAXX HOST • PYTHON • FLASK • NODE.JS • STATIC • FILE HOSTING •
          </textPath>
        </text>
      </svg>
      <div class="logo-core">R</div>
    </div>

    <h1>RAXX HOST</h1>
    <p class="login-sub">Apps · Websites · File Hosting</p>

    {% if error %}<div class="login-error">{{ error }}</div>{% endif %}

    <form method="POST" action="/login">
      <div class="form-group">
        <label>Username</label>
        <input type="text" name="username" placeholder="NGRAXX" required autofocus>
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" placeholder="••••••••" required>
      </div>
      <button type="submit" class="btn btn-primary btn-block">Sign In</button>
    </form>
    <div class="login-hint">Admin: <b>{{ admin_user }}</b></div>
  </div>
</div>

{% else %}
<div id="app">
  <div class="overlay" id="overlay" onclick="toggleMenu()"></div>

  <aside class="sidebar" id="sidebar">
    <div class="sidebar-brand">
      <div class="brand-orbit">
        <svg viewBox="0 0 38 38">
          <defs>
            <path id="miniOrbit" d="M 19,19 m -17,0 a 17,17 0 1,1 34,0 a 17,17 0 1,1 -34,0"/>
          </defs>
          <text><textPath href="#miniOrbit" startOffset="0">R A X X • R A X X •</textPath></text>
        </svg>
        <div class="brand-mark">R</div>
      </div>
      <div class="brand-text"><b>RAXX HOST</b><span>CONSOLE</span></div>
    </div>

    <nav class="nav">
      <button class="nav-item active" data-page="dashboard"><span class="icon">◈</span> Dashboard</button>
      <button class="nav-item" data-page="apps"><span class="icon">▤</span> Applications</button>
      <button class="nav-item" data-page="files"><span class="icon">◫</span> File Hosting</button>
      <button class="nav-item" data-page="logs"><span class="icon">▣</span> System Logs</button>
      <button class="nav-item" data-page="settings"><span class="icon">⚙</span> Settings</button>
    </nav>

    <div class="sidebar-user">
      <div class="user-avatar">NG</div>
      <div class="user-info"><b>{{ username }}</b><span>Administrator</span></div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="topbar-left">
        <button class="menu-btn" onclick="toggleMenu()">☰</button>
        <div class="tb-orbit">
          <svg viewBox="0 0 44 44">
            <defs>
              <path id="tbOrbit" d="M 22,22 m -19,0 a 19,19 0 1,1 38,0 a 19,19 0 1,1 -38,0"/>
            </defs>
            <text><textPath href="#tbOrbit" startOffset="0">RAXX • HOST •</textPath></text>
          </svg>
          <div class="tb-core">R</div>
        </div>
        <div>
          <h1 id="pageTitle">Dashboard</h1>
          <div class="topbar-sub" id="pageSub">Overview of your hosting platform</div>
        </div>
      </div>
      <div class="topbar-right">
        <button class="theme-btn" id="themeBtn" onclick="toggleTheme()" title="Toggle theme">🌙</button>
        <a href="/logout" class="btn btn-secondary btn-sm">⏻ Logout</a>
      </div>
    </div>

    <!-- DASHBOARD -->
    <section class="page active" id="page-dashboard">
      <div class="hero">
        <div class="hero-ring">
          <svg viewBox="0 0 230 230">
            <defs>
              <path id="heroOrbit" d="M 115,115 m -92,0 a 92,92 0 1,1 184,0 a 92,92 0 1,1 -184,0"/>
            </defs>
            <text><textPath href="#heroOrbit" startOffset="0">CONTROL • PLANE • SYSTEM • ONLINE • LIVE •</textPath></text>
          </svg>
          <div class="hero-center">
            <h2>System<br>online</h2>
          </div>
        </div>
        <div class="hero-body">
          <h3>Every app gets a public path</h3>
          <p>Deploy Python, Flask, Node.js or static sites. Each one is reachable at <strong>/p/&lt;name&gt;/</strong> on this same domain, so links work on Render without exposing internal ports.</p>
        </div>
      </div>

      <div class="stats">
        <div class="stat">
          <div class="stat-label">⚡ CPU Usage</div>
          <div class="stat-value" id="statCpu">–</div>
          <div class="stat-bar"><div class="stat-bar-fill" id="statCpuBar" style="width:0"></div></div>
        </div>
        <div class="stat">
          <div class="stat-label">▦ Memory</div>
          <div class="stat-value" id="statMem">–</div>
          <div class="stat-bar"><div class="stat-bar-fill" id="statMemBar" style="width:0"></div></div>
        </div>
        <div class="stat">
          <div class="stat-label">▶ Running Apps</div>
          <div class="stat-value" id="statRunning">–</div>
        </div>
        <div class="stat">
          <div class="stat-label">◫ Hosted Files</div>
          <div class="stat-value" id="statFiles">–</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">Quick Actions <span class="right">Jump to features</span></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn btn-primary" onclick="quickSample()">🚀 Create Sample App</button>
          <button class="btn btn-secondary" onclick="go('apps')">▤ Manage Applications</button>
          <button class="btn btn-secondary" onclick="go('files')">◫ File Hosting</button>
          <button class="btn btn-secondary" onclick="go('logs')">▣ View Logs</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">Recent Activity</div>
        <div id="recentLogs" style="font-family:'SF Mono',monospace;font-size:12px;color:#a0a0b0">Loading…</div>
      </div>
    </section>

    <!-- APPS -->
    <section class="page" id="page-apps">
      <div class="card">
        <div class="card-title">Deploy New Application <span class="right">.py · .js · .html · .zip</span></div>
        <form id="deployForm" onsubmit="return deployApp(event)">
          <div class="form-row">
            <div>
              <label class="form-label">App Name</label>
              <input type="text" name="name" class="input-text" placeholder="my-flask-app" required>
            </div>
            <div>
              <label class="form-label">Upload File</label>
              <label class="file-pick" id="pickMain" for="appFile">
                <span class="fp-icon">⬆</span>
                <span class="fp-text">
                  <b>Choose file</b>
                  <span id="appFileName">No file selected</span>
                </span>
                <input type="file" name="file" id="appFile" accept=".py,.js,.html,.zip" hidden required>
              </label>
            </div>
          </div>
          <div class="form-row">
            <div>
              <label class="form-label">requirements.txt (optional)</label>
              <label class="file-pick" id="pickReq" for="appReq">
                <span class="fp-icon">📋</span>
                <span class="fp-text">
                  <b>Choose file</b>
                  <span id="appReqName">No file selected</span>
                </span>
                <input type="file" name="requirements" id="appReq" accept=".txt" hidden>
              </label>
            </div>
            <div style="display:flex;align-items:flex-end;gap:8px">
              <button type="submit" class="btn btn-primary" id="deployBtn" style="flex:1">⬆ Deploy</button>
              <button type="button" class="btn btn-secondary" onclick="quickSample()">🎯 Sample</button>
            </div>
          </div>
          <div style="font-size:11.5px;color:#8b8b9e;margin-top:10px;line-height:1.7">
            <strong>Auto-detect:</strong> .py with Flask → Flask runner · .py → Python · .js → Node.js · .html → Static site · .zip → pick file to run<br>
            Each app is served at <strong>/p/&lt;name&gt;/</strong> on this domain. Ports stay internal and are editable.
          </div>
        </form>
      </div>

      <div class="card">
        <div class="card-title">Applications <span class="right" id="appsCount">– apps</span></div>
        <div class="app-list" id="appList">
          <div class="empty">
            <div class="empty-icon">📦</div>
            <h3>No applications yet</h3>
            <p>Deploy your first app using the form above</p>
          </div>
        </div>
      </div>
    </section>

    <!-- FILE HOSTING -->
    <section class="page" id="page-files">
      <div class="dropzone" id="fileDropzone">
        <span class="dz-icon">⬆</span>
        <p>Drop files here or click to upload</p>
        <span>Any file type · Public URL generated automatically</span>
        <input type="file" id="hostFileInput" hidden multiple>
      </div>
      <div class="card">
        <div class="card-title">Hosted Files <span class="right" id="filesCount">– files</span></div>
        <div id="hostFileList">
          <div class="empty">
            <div class="empty-icon">📁</div>
            <h3>No files hosted yet</h3>
            <p>Upload any file — images, docs, archives</p>
          </div>
        </div>
      </div>
    </section>

    <!-- LOGS -->
    <section class="page" id="page-logs">
      <div class="log-toolbar">
        <button class="filter-chip active" data-level="ALL">All</button>
        <button class="filter-chip" data-level="INFO">Info</button>
        <button class="filter-chip" data-level="SUCCESS">Success</button>
        <button class="filter-chip" data-level="WARN">Warn</button>
        <button class="filter-chip" data-level="ERROR">Error</button>
        <button class="btn btn-secondary btn-sm" style="margin-left:auto" onclick="clearLogs()">🗑 Clear</button>
      </div>
      <div class="terminal" id="terminal"></div>
    </section>

    <!-- SETTINGS -->
    <section class="page" id="page-settings">
      <div class="card">
        <div class="card-title">Change Password</div>
        <form id="passwordForm" onsubmit="return changePassword(event)">
          <div class="form-row">
            <div>
              <label class="form-label">Current Password</label>
              <input type="password" name="old" class="input-text" placeholder="Enter current" required>
            </div>
            <div>
              <label class="form-label">New Password</label>
              <input type="password" name="new" class="input-text" placeholder="Min 4 chars" required>
            </div>
          </div>
          <button type="submit" class="btn btn-primary">Update Password</button>
        </form>
      </div>

      <div class="card">
        <div class="card-title">System Information</div>
        <div id="sysInfo" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px"></div>
      </div>
    </section>
  </main>
</div>

<!-- LOGS MODAL -->
<div class="modal-backdrop" id="logModal">
  <div class="modal">
    <div class="modal-header">
      <h3 id="logTitle">App Logs</h3>
      <button class="btn btn-secondary btn-sm" onclick="closeModal('logModal')">Close</button>
    </div>
    <div class="modal-body"><pre id="logContent">Loading…</pre></div>
  </div>
</div>

<!-- ENDPOINT MODAL -->
<div class="modal-backdrop" id="endpointModal">
  <div class="modal modal-md">
    <div class="modal-header">
      <h3>⚙ Edit Endpoint</h3>
      <button class="btn btn-secondary btn-sm" onclick="closeModal('endpointModal')">Close</button>
    </div>
    <div class="modal-body">
      <div class="path-box">
        <span style="color:#8b8b9e">PATH</span>
        <span id="epPath">—</span>
      </div>

      <p style="color:#8b8b9e;font-size:12.5px;margin-bottom:16px;line-height:1.6">
        The path above is the public link on this domain — it always works, including on Render. The port is internal only; change it if it clashes.
      </p>

      <label class="form-label">Internal Port</label>
      <input type="number" id="epPort" class="input-text" min="1024" max="65535" placeholder="8080">
      <div id="epHint" style="margin-top:8px;font-size:11.5px;color:#8b8b9e"></div>

      <label class="form-label" style="margin-top:16px">Public URL override (optional)</label>
      <input type="text" id="epUrl" class="input-text" placeholder="https://myapp.example.com">
      <div style="font-size:11.5px;color:#8b8b9e;margin-top:6px;line-height:1.6">
        Only for a custom domain pointed at this server. Leave empty to use the path link.
      </div>

      <div class="endpoint-preview">
        <span class="ep-label">Link</span>
        <span id="epPreview">—</span>
      </div>
      <div class="ep-actions">
        <button class="btn btn-secondary btn-sm" onclick="copyEndpointPreview()">🔗 Copy</button>
        <button class="btn btn-secondary btn-sm" onclick="openEndpointPreview()">↗ Open</button>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('endpointModal')">Cancel</button>
      <button class="btn btn-primary" id="epSaveBtn" onclick="saveEndpoint()">Save</button>
    </div>
  </div>
</div>

<!-- ZIP FILE PICKER MODAL -->
<div class="modal-backdrop" id="zipModal">
  <div class="modal modal-lg">
    <div class="modal-header">
      <h3>📦 Choose Entry File</h3>
      <button class="btn btn-secondary btn-sm" onclick="cancelZip()">Cancel</button>
    </div>
    <div class="modal-body">
      <div id="zipSummary" class="zip-summary"></div>

      <div class="zip-section-title">▶ Suggested Entry Points</div>
      <div class="zip-file-list" id="zipEntries"></div>

      <div class="zip-section-title">📄 All Files (<span id="zipFileTotal">0</span>)</div>
      <div class="zip-file-list" id="zipAllFiles"></div>

      <div style="margin-top:14px">
        <label class="form-label">App Name</label>
        <input type="text" id="zipAppName" class="input-text" placeholder="my-app" required>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="cancelZip()">Cancel</button>
      <button class="btn btn-primary" id="zipConfirmBtn" onclick="confirmZip()">Deploy Selected</button>
    </div>
  </div>
</div>

<div class="toast-wrap" id="toastWrap"></div>
{% endif %}

<script>
const LEVEL_COLORS = { INFO:'#38bdf8', SUCCESS:'#10b981', WARN:'#f59e0b', ERROR:'#f43f5e' };
const TYPE_ICONS = { python:'🐍', node:'⬢', 'static':'🌐', flask:'🧪' };
const TYPE_LABELS = { python:'Python', node:'Node.js', 'static':'Static', flask:'Flask' };
const FILE_ICONS = {
  PNG:'🖼️', JPG:'🖼️', JPEG:'🖼️', GIF:'🖼️', SVG:'🎨', WEBP:'🖼️',
  PDF:'📄', DOC:'📝', DOCX:'📝', TXT:'📝', MD:'📝',
  ZIP:'📦', RAR:'📦', '7Z':'📦', TAR:'📦', GZ:'📦',
  MP4:'🎬', MP3:'🎵', WAV:'🎵', MOV:'🎬',
  JSON:'📋', XML:'📋', CSV:'📊', XLS:'📊', XLSX:'📊',
  HTML:'🌐', CSS:'🎨', JS:'⚡', PY:'🐍',
  DB:'🗄️', SQL:'🗄️', SQLITE:'🗄️'
};

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toast(msg, type) {
  type = type || 'info';
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => { el.classList.add('removing'); setTimeout(() => el.remove(), 250); }, 3200);
}

function humanSize(n) {
  for (const u of ['B','KB','MB','GB','TB']) {
    if (n < 1024) return n.toFixed(1) + ' ' + u;
    n /= 1024;
  }
  return n.toFixed(1) + ' PB';
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast('Copied', 'ok'))
    .catch(() => toast(text, 'info'));
}

/* ═══ THEME ═══ */
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('raxx_theme', next);
  document.getElementById('themeBtn').textContent = next === 'dark' ? '🌙' : '☀️';
}
(function() {
  const saved = localStorage.getItem('raxx_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = saved === 'dark' ? '🌙' : '☀️';
})();

/* ═══ NAV ═══ */
function go(page) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page));

  const TITLES = {
    dashboard: ['Dashboard', 'Overview of your hosting platform'],
    apps:      ['Applications', 'Deploy and manage apps'],
    files:     ['File Hosting', 'Upload and share any file'],
    logs:      ['System Logs', 'Server activity and events'],
    settings:  ['Settings', 'Account and system configuration']
  };
  document.getElementById('pageTitle').textContent = TITLES[page][0];
  document.getElementById('pageSub').textContent = TITLES[page][1];
  closeMenu();

  if (page === 'dashboard') loadDashboard();
  if (page === 'apps') loadApps();
  if (page === 'files') loadHostedFiles();
  if (page === 'logs') loadSystemLogs();
  if (page === 'settings') loadSettings();
}

function toggleMenu() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('overlay').classList.toggle('show');
}
function closeMenu() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('show');
}

function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }

async function api(url, opts) {
  try {
    const res = await fetch(url, opts || {});
    if (res.status === 401) { location.href = '/login'; return null; }
    return res;
  } catch (e) {
    toast('Network error: ' + e.message, 'err');
    return null;
  }
}

{% if logged_in %}

document.querySelectorAll('.nav-item').forEach(n => n.addEventListener('click', () => go(n.dataset.page)));

/* ═══ CUSTOM FILE PICKERS ═══ */
function wirePicker(pickId, inputId, labelId) {
  const pick = document.getElementById(pickId);
  const input = document.getElementById(inputId);
  const label = document.getElementById(labelId);
  if (!pick || !input) return;

  input.addEventListener('change', () => {
    const f = input.files && input.files[0];
    if (f) {
      label.textContent = f.name + ' · ' + humanSize(f.size);
      pick.classList.add('has-file');
    } else {
      label.textContent = 'No file selected';
      pick.classList.remove('has-file');
    }
  });

  pick.addEventListener('dragover', e => { e.preventDefault(); pick.classList.add('drag'); });
  pick.addEventListener('dragleave', () => pick.classList.remove('drag'));
  pick.addEventListener('drop', e => {
    e.preventDefault();
    pick.classList.remove('drag');
    if (!e.dataTransfer.files.length) return;
    const dt = new DataTransfer();
    dt.items.add(e.dataTransfer.files[0]);
    input.files = dt.files;
    input.dispatchEvent(new Event('change'));
  });
}
wirePicker('pickMain', 'appFile', 'appFileName');
wirePicker('pickReq', 'appReq', 'appReqName');

function resetPickers() {
  ['appFile','appReq'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const l1 = document.getElementById('appFileName');
  const l2 = document.getElementById('appReqName');
  if (l1) l1.textContent = 'No file selected';
  if (l2) l2.textContent = 'No file selected';
  const p1 = document.getElementById('pickMain');
  const p2 = document.getElementById('pickReq');
  if (p1) p1.classList.remove('has-file');
  if (p2) p2.classList.remove('has-file');
}

/* ═══ DASHBOARD ═══ */
async function loadDashboard() {
  const sr = await api('/api/system');
  if (sr) {
    const sys = await sr.json();
    if (sys.cpu !== null && sys.cpu !== undefined) {
      document.getElementById('statCpu').innerHTML = Math.round(sys.cpu) + '<small>%</small>';
      document.getElementById('statCpuBar').style.width = sys.cpu + '%';
    } else {
      document.getElementById('statCpu').innerHTML = 'N/A';
    }
    if (sys.memory) {
      const pct = Math.round(sys.memory.percent);
      document.getElementById('statMem').innerHTML = pct + '<small>%</small>';
      document.getElementById('statMemBar').style.width = pct + '%';
    } else {
      document.getElementById('statMem').innerHTML = 'N/A';
    }
    document.getElementById('statFiles').innerHTML = sys.files_count + '<small> files</small>';
  }

  const ar = await api('/api/apps');
  if (ar) {
    const apps = await ar.json();
    document.getElementById('statRunning').innerHTML = apps.filter(a => a.status === 'running').length + '<small> apps</small>';
  }

  const lr = await api('/api/logs?limit=8');
  if (lr) {
    const logs = await lr.json();
    const el = document.getElementById('recentLogs');
    if (!logs.length) {
      el.textContent = 'No activity yet.';
    } else {
      el.innerHTML = logs.map(l => {
        const color = LEVEL_COLORS[l.level] || '#8b8b9e';
        const time = (l.created_at || '').slice(11, 19);
        return '<div style="display:flex;gap:10px;padding:4px 0">' +
          '<span style="color:#4a4a66;flex-shrink:0">' + esc(time) + '</span>' +
          '<span style="color:' + color + ';font-weight:700;width:64px;flex-shrink:0">' + esc(l.level) + '</span>' +
          '<span style="color:#c5c5d5">' + esc(l.message) + '</span>' +
        '</div>';
      }).join('');
    }
  }
}

/* ═══ APPS ═══ */
async function loadApps() {
  const r = await api('/api/apps');
  if (!r) return;
  const apps = await r.json();
  document.getElementById('appsCount').textContent = apps.length + ' apps';

  const list = document.getElementById('appList');
  if (!apps.length) {
    list.innerHTML = '<div class="empty">' +
      '<div class="empty-icon">📦</div>' +
      '<h3>No applications yet</h3>' +
      '<p>Deploy your first app using the form above</p></div>';
    return;
  }

  list.innerHTML = apps.map(a => {
    const isRunning = a.status === 'running';
    const type = a.app_type || 'python';
    const typeIcon = TYPE_ICONS[type] || '📄';
    const typeLabel = TYPE_LABELS[type] || type;
    const shortUrl = (a.url || '').replace(/^https?:\/\//, '');

    const actions = isRunning
      ? '<button class="btn btn-secondary btn-sm" onclick="openLogs(' + a.id + ',\'' + esc(a.name) + '\')">📋 Logs</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="openEndpoint(' + a.id + ',' + a.port + ',\'' + esc(a.public_url || '') + '\',\'' + esc(a.name) + '\',\'' + esc(a.path) + '\')">⚙ Endpoint</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="doRestart(' + a.id + ',this)">🔄 Restart</button>' +
        '<button class="btn btn-danger btn-sm" onclick="doStop(' + a.id + ',this)">⏹ Stop</button>'
      : '<button class="btn btn-success btn-sm" onclick="doStart(' + a.id + ',this)">▶ Start</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="openLogs(' + a.id + ',\'' + esc(a.name) + '\')">📋 Logs</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="openEndpoint(' + a.id + ',' + a.port + ',\'' + esc(a.public_url || '') + '\',\'' + esc(a.name) + '\',\'' + esc(a.path) + '\')">⚙ Endpoint</button>';

    return '<div class="app-card">' +
      '<div class="app-header">' +
        '<div class="app-title">' +
          '<div class="app-icon">' + typeIcon + '</div>' +
          '<div style="min-width:0">' +
            '<h3>' + esc(a.name) + '</h3>' +
            '<div class="meta">' + esc(a.entry) + ' · port ' + a.port + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="app-actions">' +
          '<span class="badge ' + (isRunning ? 'running' : 'stopped') + '">' +
            '<span class="dot"></span>' + (isRunning ? 'Running' : 'Stopped') + '</span>' +
          '<span class="badge type ' + type + '">' + typeLabel + '</span>' +
          actions +
          '<button class="btn btn-danger btn-sm" onclick="doDelete(' + a.id + ',\'' + esc(a.name) + '\')">🗑</button>' +
        '</div>' +
      '</div>' +
      '<div class="app-footer">' +
        '<button class="badge port" onclick="window.open(\'' + esc(a.url) + '\',\'_blank\')" title="Open link">🔌 ' + esc(shortUrl) + '</button>' +
        '<button class="badge port" onclick="copyText(\'' + esc(a.url) + '\')" title="Copy link">🔗 Copy</button>' +
        (a.pid ? '<span><strong>PID:</strong> ' + a.pid + '</span>' : '') +
        '<span><strong>Created:</strong> ' + esc((a.created_at || '').slice(0, 10)) + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

async function deployApp(e) {
  e.preventDefault();
  const form = e.target;
  const name = form.name.value.trim();
  const file = document.getElementById('appFile').files[0];
  if (!name || !file) { toast('Name + file required', 'err'); return false; }

  const btn = document.getElementById('deployBtn');
  const old = btn.innerHTML;

  if (file.name.toLowerCase().endsWith('.zip')) {
    btn.disabled = true;
    btn.innerHTML = '<span>⏳</span> Analyzing…';

    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch('/api/apps/analyze-zip', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) {
        toast(data.error || 'Analysis failed', 'err');
        btn.disabled = false; btn.innerHTML = old;
        return false;
      }
      showZipPicker(data, name);
    } catch (err) {
      toast('Upload failed: ' + err.message, 'err');
    } finally {
      btn.disabled = false; btn.innerHTML = old;
    }
    return false;
  }

  btn.disabled = true;
  btn.innerHTML = '<span>⏳</span> Deploying…';

  const fd = new FormData(form);
  try {
    const r = await fetch('/api/apps/create-direct', { method: 'POST', body: fd });
    const data = await r.json();
    if (r.ok) {
      toast('Deployed at ' + data.path, 'ok');
      if (data.installed && data.installed.length)
        toast('Installed: ' + data.installed.join(', '), 'info');
      form.reset();
      resetPickers();
      loadApps();
    } else {
      toast(data.error || 'Deploy failed', 'err');
    }
  } catch (err) {
    toast('Network error: ' + err.message, 'err');
  } finally {
    btn.disabled = false; btn.innerHTML = old;
  }
  return false;
}

/* ═══ ZIP PICKER ═══ */
let zipData = null;
let zipSelected = null;

function showZipPicker(data, suggestedName) {
  zipData = data;
  zipSelected = data.entries.length ? data.entries[0] : null;

  document.getElementById('zipSummary').innerHTML =
    '<strong>' + data.file_count + ' files</strong> · ' +
    '<strong>' + humanSize(data.total_size) + '</strong> total · ' +
    '<strong>' + data.entries.length + '</strong> runnable entries found';

  document.getElementById('zipFileTotal').textContent = data.files.length;

  document.getElementById('zipEntries').innerHTML = data.entries.length
    ? data.entries.map((e, i) => {
        const kindIcon = e.type === 'flask' ? '🧪' : e.type === 'python' ? '🐍' : e.type === 'node' ? '⬢' : '🌐';
        const isSel = zipSelected && zipSelected.path === e.path && zipSelected.type === e.type;
        return '<div class="zip-file-item ' + (isSel ? 'selected' : '') + '" ' +
          'onclick="selectZipEntry(' + i + ')">' +
          '<span class="kind-icon">' + kindIcon + '</span>' +
          '<span class="fpath">' + esc(e.path) + '</span>' +
          '<span class="run-tag">Run ' + esc(e.type) + '</span>' +
        '</div>';
      }).join('')
    : '<div style="padding:16px;color:#8b8b9e;font-size:12.5px">No runnable files detected (need .py, .js, or index.html)</div>';

  document.getElementById('zipAllFiles').innerHTML = data.files.map(f => {
    const key = f.kind === 'flask' ? 'PY' : f.kind === 'python' ? 'PY' : f.kind === 'node' ? 'JS' : f.kind === 'html' ? 'HTML' : '';
    const icon = FILE_ICONS[key] || '📄';
    return '<div class="zip-file-item" style="cursor:default">' +
      '<span class="kind-icon">' + icon + '</span>' +
      '<span class="fpath">' + esc(f.path) + '</span>' +
      '<span class="fsize">' + humanSize(f.size) + '</span>' +
    '</div>';
  }).join('');

  document.getElementById('zipAppName').value = suggestedName || '';
  openModal('zipModal');
}

function selectZipEntry(i) {
  zipSelected = zipData.entries[i];
  document.querySelectorAll('#zipEntries .zip-file-item').forEach((el, idx) => {
    el.classList.toggle('selected', idx === i);
  });
}

async function confirmZip() {
  if (!zipSelected) { toast('Select a file to run', 'err'); return; }
  const name = document.getElementById('zipAppName').value.trim();
  if (!name) { toast('Enter app name', 'err'); return; }

  const btn = document.getElementById('zipConfirmBtn');
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>⏳</span> Deploying…';

  try {
    const r = await fetch('/api/apps/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        temp_id: zipData.temp_id,
        name: name,
        entry: zipSelected.path,
        app_type: zipSelected.type
      })
    });
    const data = await r.json();
    if (r.ok) {
      toast('Deployed at ' + data.path, 'ok');
      if (data.installed && data.installed.length)
        toast('Installed: ' + data.installed.join(', '), 'info');
      closeModal('zipModal');
      document.getElementById('deployForm').reset();
      resetPickers();
      loadApps();
    } else {
      toast(data.error || 'Deploy failed', 'err');
    }
  } catch (err) {
    toast('Network error: ' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

function cancelZip() {
  closeModal('zipModal');
  zipData = null;
  zipSelected = null;
}

async function quickSample() {
  const r = await api('/api/apps/sample', { method: 'POST' });
  if (!r) return;
  const data = await r.json();
  if (r.ok) {
    toast('Sample created at ' + data.path, 'ok');
    if (document.getElementById('page-dashboard').classList.contains('active')) loadDashboard();
    if (document.getElementById('page-apps').classList.contains('active')) loadApps();
  } else {
    toast(data.error || 'Failed', 'err');
  }
}

/* ═══ APP ACTIONS ═══ */
async function doStart(id, btn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>⏳</span> Starting…';
  const r = await api('/api/apps/' + id + '/start', { method: 'POST' });
  if (!r) { btn.disabled = false; btn.innerHTML = old; return; }
  const d = await r.json();
  if (r.ok) { toast('Started (PID ' + d.pid + ')', 'ok'); loadApps(); }
  else { toast(d.error || 'Start failed', 'err'); btn.disabled = false; btn.innerHTML = old; }
}

async function doStop(id, btn) {
  if (!confirm('Stop this application?')) return;
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>⏳</span> Stopping…';
  const r = await api('/api/apps/' + id + '/stop', { method: 'POST' });
  if (!r) { btn.disabled = false; btn.innerHTML = old; return; }
  const d = await r.json();
  if (r.ok) { toast('Stopped', 'ok'); loadApps(); }
  else { toast(d.error || 'Stop failed', 'err'); btn.disabled = false; btn.innerHTML = old; }
}

async function doRestart(id, btn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>⏳</span> Restarting…';
  const r = await api('/api/apps/' + id + '/restart', { method: 'POST' });
  if (!r) { btn.disabled = false; btn.innerHTML = old; return; }
  const d = await r.json();
  if (r.ok) { toast('Restarted (PID ' + d.pid + ')', 'ok'); loadApps(); }
  else { toast(d.error || 'Restart failed', 'err'); btn.disabled = false; btn.innerHTML = old; }
}

async function doDelete(id, name) {
  if (!confirm('Delete "' + name + '"? Files will be removed permanently.')) return;
  const r = await api('/api/apps/' + id, { method: 'DELETE' });
  if (!r) return;
  if (r.ok) { toast('Deleted', 'ok'); loadApps(); }
  else { const d = await r.json(); toast(d.error || 'Delete failed', 'err'); }
}

/* ═══ ENDPOINT EDIT ═══ */
let editingAppId = null;
let editingPath = '';

function buildPreview() {
  const port = document.getElementById('epPort').value.trim();
  const url = document.getElementById('epUrl').value.trim();
  if (url) return url.replace(/\/$/, '');
  if (editingPath) return location.origin + editingPath;
  return location.origin + ':' + (port || '—');
}

function refreshPreview() {
  document.getElementById('epPreview').textContent = buildPreview();
}

function openEndpoint(appId, port, publicUrl, name, path) {
  editingAppId = appId;
  editingPath = path || '';
  document.getElementById('epPort').value = port;
  document.getElementById('epUrl').value = publicUrl || '';
  document.getElementById('epPath').textContent = editingPath || '—';
  document.getElementById('epHint').textContent = 'Current port: ' + port + ' · App: ' + name;
  refreshPreview();
  openModal('endpointModal');
  setTimeout(() => document.getElementById('epPort').focus(), 100);
}

document.getElementById('epPort').addEventListener('input', refreshPreview);
document.getElementById('epUrl').addEventListener('input', refreshPreview);

function copyEndpointPreview() { copyText(buildPreview()); }
function openEndpointPreview() { window.open(buildPreview(), '_blank'); }

async function saveEndpoint() {
  if (!editingAppId) return;
  const port = parseInt(document.getElementById('epPort').value, 10);
  if (!port || port < 1024 || port > 65535) {
    toast('Port must be 1024-65535', 'err');
    return;
  }
  const url = document.getElementById('epUrl').value.trim();

  const btn = document.getElementById('epSaveBtn');
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>⏳</span> Saving…';

  try {
    const r = await api('/api/apps/' + editingAppId + '/endpoint', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ port: port, public_url: url })
    });
    if (!r) { btn.disabled = false; btn.innerHTML = old; return; }
    const data = await r.json();
    if (r.ok) {
      toast('Endpoint saved' + (data.restarted ? ' · restarted' : ''), 'ok');
      closeModal('endpointModal');
      editingAppId = null;
      loadApps();
    } else {
      toast(data.error || 'Failed', 'err');
    }
  } catch (err) {
    toast('Network error: ' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

/* ═══ APP LOGS ═══ */
let logPoll = null;

async function openLogs(appId, name) {
  document.getElementById('logTitle').textContent = '📋 Logs — ' + name;
  document.getElementById('logContent').textContent = 'Loading…';
  document.getElementById('logModal').dataset.appId = appId;
  openModal('logModal');
  await refreshAppLogs();
  if (logPoll) clearInterval(logPoll);
  logPoll = setInterval(refreshAppLogs, 2000);
}

async function refreshAppLogs() {
  const appId = document.getElementById('logModal').dataset.appId;
  if (!appId) return;
  try {
    const r = await fetch('/api/apps/' + appId + '/logs');
    if (!r.ok) return;
    const data = await r.json();
    const c = document.getElementById('logContent');
    c.textContent = data.logs || 'No logs yet.';
    c.scrollTop = c.scrollHeight;
  } catch (e) {}
}

document.getElementById('logModal').addEventListener('click', function(e) {
  if (e.target === this) {
    closeModal('logModal');
    document.getElementById('logModal').dataset.appId = '';
    if (logPoll) { clearInterval(logPoll); logPoll = null; }
  }
});

/* ═══ FILE HOSTING ═══ */
async function loadHostedFiles() {
  const r = await api('/api/files');
  if (!r) return;
  const files = await r.json();
  document.getElementById('filesCount').textContent = files.length + ' files';

  const list = document.getElementById('hostFileList');
  if (!files.length) {
    list.innerHTML = '<div class="empty">' +
      '<div class="empty-icon">📁</div>' +
      '<h3>No files hosted yet</h3>' +
      '<p>Upload any file — images, docs, archives</p></div>';
    return;
  }

  list.innerHTML = files.map(f => {
    const icon = FILE_ICONS[f.filetype] || '📄';
    const url = '/files/' + f.id;
    return '<div class="file-row">' +
      '<div class="file-icon">' + icon + '</div>' +
      '<div class="file-info">' +
        '<b>' + esc(f.filename) + '</b>' +
        '<span>' + humanSize(f.filesize) + ' · ' + esc(f.filetype) + ' · ' +
          esc((f.uploaded_at || '').slice(0, 10)) + '</span>' +
      '</div>' +
      '<div class="file-actions">' +
        '<button class="btn btn-secondary btn-sm" onclick="copyUrl(\'' + url + '\')">🔗</button>' +
        '<a href="' + url + '" target="_blank" class="btn btn-secondary btn-sm">👁</a>' +
        '<a href="' + url + '/download" class="btn btn-secondary btn-sm">⬇</a>' +
        '<button class="btn btn-danger btn-sm" onclick="deleteHostFile(' + f.id + ',\'' + esc(f.filename) + '\')">🗑</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

async function uploadHostedFiles(fileList) {
  let ok = 0, fail = 0;
  for (const f of fileList) {
    const fd = new FormData();
    fd.append('file', f);
    try {
      const r = await fetch('/api/files/upload', { method: 'POST', body: fd });
      if (r.ok) ok++;
      else { fail++; const d = await r.json(); toast(d.error || 'Upload failed: ' + f.name, 'err'); }
    } catch (e) { fail++; toast('Upload error: ' + f.name, 'err'); }
  }
  if (ok > 0) toast(ok + ' file' + (ok > 1 ? 's' : '') + ' uploaded', 'ok');
  loadHostedFiles();
}

async function deleteHostFile(id, name) {
  if (!confirm('Delete "' + name + '"?')) return;
  const r = await api('/api/files/' + id, { method: 'DELETE' });
  if (!r) return;
  if (r.ok) { toast('Deleted', 'ok'); loadHostedFiles(); }
  else { const d = await r.json(); toast(d.error || 'Delete failed', 'err'); }
}

function copyUrl(path) {
  copyText(location.origin + path);
}

/* Dropzone */
const dz = document.getElementById('fileDropzone');
const fileInput = document.getElementById('hostFileInput');
dz.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', e => uploadHostedFiles(e.target.files));
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag');
  uploadHostedFiles(e.dataTransfer.files);
});

/* ═══ SYSTEM LOGS ═══ */
let logFilter = 'ALL';

async function loadSystemLogs() {
  const r = await api('/api/logs?level=' + logFilter + '&limit=300');
  if (!r) return;
  const logs = await r.json();
  const term = document.getElementById('terminal');
  if (!logs.length) {
    term.innerHTML = '<div style="color:#4a4a66;padding:20px;text-align:center">No logs for this filter.</div>';
    return;
  }
  term.innerHTML = logs.map(l => {
    const time = (l.created_at || '').slice(11, 19);
    return '<div class="log-row">' +
      '<span class="lt">' + esc(time) + '</span>' +
      '<span class="ll ' + esc(l.level) + '">' + esc(l.level) + '</span>' +
      '<span class="lm">' + esc(l.message) + '</span>' +
    '</div>';
  }).join('');
}

document.querySelectorAll('.filter-chip[data-level]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-chip[data-level]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    logFilter = btn.dataset.level;
    loadSystemLogs();
  });
});

async function clearLogs() {
  if (!confirm('Clear all system logs?')) return;
  const r = await api('/api/logs', { method: 'DELETE' });
  if (!r) return;
  if (r.ok) { toast('Logs cleared', 'ok'); loadSystemLogs(); }
}

/* ═══ SETTINGS ═══ */
async function loadSettings() {
  const r = await api('/api/system');
  if (!r) return;
  const sys = await r.json();

  const items = [
    ['Hostname', sys.hostname],
    ['OS', sys.os],
    ['Python', sys.python],
    ['Public base', sys.base || 'local host'],
    ['psutil', sys.psutil ? '✓ Installed' : '✗ Missing'],
    ['CPU', sys.cpu !== null && sys.cpu !== undefined ? Math.round(sys.cpu) + '%' : 'N/A'],
    ['Memory', sys.memory ? Math.round(sys.memory.percent) + '%' : 'N/A'],
    ['Disk', sys.disk ? Math.round(sys.disk.percent) + '%' : 'N/A'],
    ['Apps', sys.apps_count],
    ['Files', sys.files_count + ' (' + humanSize(sys.files_size) + ')'],
  ];

  document.getElementById('sysInfo').innerHTML = items.map(([k, v]) =>
    '<div class="sys-item" style="padding:12px 14px;background:#0a0a0f;border:1px solid #23232d;border-radius:9px">' +
      '<div style="font-size:10.5px;color:#8b8b9e;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-bottom:5px">' + esc(k) + '</div>' +
      '<div style="font-size:13px;font-weight:600">' + esc(v) + '</div>' +
    '</div>'
  ).join('');
}

async function changePassword(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await api('/api/password', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ old: fd.get('old'), new: fd.get('new') })
  });
  if (!r) return false;
  const data = await r.json();
  if (r.ok) { toast('Password updated', 'ok'); e.target.reset(); }
  else { toast(data.error || 'Failed', 'err'); }
  return false;
}

/* ═══ BOOT ═══ */
loadDashboard();
setInterval(() => {
  if (document.getElementById('page-dashboard').classList.contains('active')) loadDashboard();
}, 15000);

{% endif %}
</script>
</body>
</html>
'''

# ════════════════════ MAIN ════════════════════
if __name__ == '__main__':
    init_db()
    sync_app_statuses()
    for d in os.listdir(TEMP_DIR):
        try:
            shutil.rmtree(os.path.join(TEMP_DIR, d), ignore_errors=True)
        except Exception:
            pass

    port = int(os.environ.get('PORT', '5000'))
    base = external_base() or f'http://localhost:{port}'

    print('\n' + '═' * 62)
    print('  RAXX HOST v6 — Apps + Websites + File Hosting')
    print('═' * 62)
    print('  Listen   : 0.0.0.0:' + str(port))
    print('  Base URL : ' + base)
    print('  Username : ' + ADMIN_USERNAME)
    print('  Password : ' + ADMIN_PASSWORD)
    print('─' * 62)
    print('  App links: ' + base + '/p/<slug>/   ← works on Render')
    print('  Uploads  : .py · .js · .html · .zip (auto-detect)')
    print('  Flask    : auto-detected, runs via flask runner on PORT')
    print('  Endpoint : port (internal) + public URL override')
    print('  Files    : unlimited hosting, public URLs')
    print('═' * 62 + '\n')

    try:
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    finally:
        cleanup()