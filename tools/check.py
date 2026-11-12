# -*- coding: utf-8 -*-
"""
Единая проверка проекта CYBER-VAULT.

    python tools\\check.py          # всё: статика + эндпоинты + параллельность
    python tools\\check.py --static # только статика (быстро, без правки vault.json)

Что проверяет:
  1. app.py компилируется
  2. pyflakes: нет неопределённых имён / неиспользуемых импортов
  3. встроенный JS: синтаксис (node --check) + нет вызываемых-но-не-определённых функций
  4. все HTTP-эндпоинты живы (валидные данные, битые данные, мусор в теле)
  5. сервер параллельный (8 медленных ping -> ~5 с, GET во время них -> быстро)

Тестовые записи создаются и удаляются сами; в vault.json остаётся мусор
с префиксом id "__audit_", если тест был прерван — удалить их можно так:
    python tools\\check.py --cleanup
"""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
BASE = "http://127.0.0.1:17555"
PFX = "__audit_"

failures = []
notes = []


def fail(msg):
    failures.append(msg)
    print("  FAIL | " + msg)


def ok(msg):
    print("  ok   | " + msg)


def info(msg):
    print("  ..   | " + msg)


# --------------------------------------------------------------------------- HTTP
def http(path, body=None, raw=None, method=None, timeout=30):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers,
                               method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "EXC: %r" % (e,)


def api_check(name, path, body=None, raw=None, method=None, expect_json=True, want=None):
    st, txt = http(path, body, raw, method)
    if st == 0:
        fail("%s — нет связи с приложением (%s)" % (name, txt[:60]))
        return None
    parsed = None
    if expect_json:
        try:
            parsed = json.loads(txt)
        except Exception:
            fail("%s — ответ не JSON: %s" % (name, txt[:60]))
            return None
        if want is not None and isinstance(parsed, dict) and parsed.get("ok") != want:
            fail("%s — ok=%s, ожидалось %s (%s)" % (name, parsed.get("ok"), want, str(parsed.get("msg"))[:60]))
            return parsed
    ok("%s — HTTP %s" % (name, st))
    return parsed


# --------------------------------------------------------------------------- static
def check_static():
    print("\n[1/5] Компиляция app.py")
    r = subprocess.run([sys.executable, "-m", "py_compile", APP], capture_output=True, text=True)
    if r.returncode == 0:
        ok("py_compile")
    else:
        fail("py_compile: %s" % r.stderr.strip()[:300])

    print("\n[2/5] pyflakes")
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes", APP], capture_output=True, text=True)
        if r.returncode == 0:
            ok("pyflakes — замечаний нет")
        else:
            fail("pyflakes:\n" + r.stdout.strip()[:800])
    except Exception as e:
        info("pyflakes недоступен (%s) — пропуск" % str(e)[:60])

    print("\n[3/5] Встроенный JavaScript")
    src = io.open(APP, encoding="utf-8").read()
    try:
        start = src.index('HTML_UI = r"""') + len('HTML_UI = r"""')
        end = src.index('"""', start)
    except ValueError:
        fail("не найден блок HTML_UI — файл повреждён?")
        return
    html = src[start:end]
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    tmp = os.path.join(tempfile.gettempdir(), "cybervault_check_ui_%d.js" % os.getpid())
    try:
        io.open(tmp, "w", encoding="utf-8").write(js)
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            if r.returncode == 0:
                ok("node --check")
            else:
                fail("node --check: %s" % r.stderr.strip()[:400])
        else:
            info("node не найден — синтаксис JS не проверен")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    declared = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    declared |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", js))
    builtin = set("""if for while switch catch function return typeof Math JSON Object Array String Number
        Boolean Date Promise Map Set RegExp Error parseInt parseFloat setTimeout setInterval clearTimeout
        clearInterval fetch alert confirm prompt encodeURIComponent decodeURIComponent isNaN
        structuredClone requestAnimationFrame""".split())
    called = set(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", js))
    missing = sorted(c for c in called if c not in declared and c not in builtin and c != "async")
    if missing:
        fail("JS: вызываются, но не определены: %s" % ", ".join(missing))
    else:
        ok("JS: все вызываемые функции определены")

    onclick = set()
    for m in re.findall(r"on(?:click|change)=\"([^\"]*)\"", js):
        onclick |= set(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", m))
    miss2 = sorted(f for f in onclick if f not in declared and f not in builtin)
    if miss2:
        fail("JS: обработчики onclick без определения: %s" % ", ".join(miss2))
    else:
        ok("JS: все onclick-обработчики определены")


# --------------------------------------------------------------------------- sweep
def check_endpoints():
    print("\n[4/5] HTTP-эндпоинты")
    audit_dir = os.path.join(ROOT, "_audit_tmp")
    os.makedirs(audit_dir, exist_ok=True)
    io.open(os.path.join(audit_dir, ".env"), "w", encoding="utf-8").write(
        "AUDIT_TOKEN=secret123\nOTHER=value\n")

    api_check("GET /", "/", expect_json=False)
    api_check("GET /index", "/index", expect_json=False)
    api_check("GET /api/get_data", "/api/get_data")
    api_check("GET /api/poll_clipboard", "/api/poll_clipboard")
    st, _ = http("/api/nope")
    (ok if st == 404 else fail)("GET неизвестного пути -> %s" % st)

    api_check("save_key", "/api/save_key", {"id": PFX + "k1", "service": "AuditProv",
                                            "env_name": "AUDIT_KEY", "category": "ai",
                                            "value": "audit-value-1", "base_url": "https://audit.test/v1"})
    api_check("save_key (2-й ключ)", "/api/save_key", {"id": PFX + "k2", "service": "AuditProv",
                                                      "env_name": "AUDIT_KEY_2", "category": "ai",
                                                      "value": "audit-value-2"})
    api_check("save_key без полей", "/api/save_key", {"id": PFX + "k3"}, want=False)
    api_check("save_app", "/api/save_app", {"id": PFX + "app", "name": "AuditAgent", "icon": "x",
                                            "path": r"C:\Windows\System32\notepad.exe",
                                            "linked_keys": [PFX + "k1", PFX + "k2"]})
    api_check("save_personal", "/api/save_personal", {"id": PFX + "p", "provider": "Audit",
                                                      "value": "audit-secret", "notes": "n"})
    api_check("set_primary", "/api/set_primary", {"id": PFX + "k2", "primary": True})
    api_check("set_rotation", "/api/set_rotation", {"id": PFX + "k1", "rotation": True})
    api_check("ignore_token", "/api/ignore_token", {"token": "audit-value-1"})
    api_check("toggle_clip_monitor", "/api/toggle_clip_monitor", {"enabled": False})
    api_check("toggle_clip_monitor обратно", "/api/toggle_clip_monitor", {"enabled": True})
    api_check("grab_clipboard", "/api/grab_clipboard", {})
    api_check("ping (существующий)", "/api/ping", {"id": PFX + "k1"})
    api_check("ping (несуществующий)", "/api/ping", {"id": "нет-такого"}, want=False)
    api_check("launch_app (файл)", "/api/launch_app", {"path": r"C:\Windows\System32\notepad.exe",
                                                      "app_id": PFX + "app"})
    api_check("launch_app (нет команды)", "/api/launch_app", {"path": "no_such_cmd_zzz"}, want=False)
    api_check("launch_app (пусто)", "/api/launch_app", {"path": ""}, want=False)
    api_check("agent_icon (exe)", "/api/agent_icon", {"path": r"C:\Windows\System32\notepad.exe"})
    api_check("agent_icon (папка)", "/api/agent_icon", {"path": os.path.join(ROOT, "libs")}, want=False)
    api_check("open_url", "/api/open_url", {"url": "about:blank"})
    api_check("open_url (пусто)", "/api/open_url", {"url": ""}, want=False)
    api_check("setx", "/api/setx", {"env_name": "CYBERVAULT_AUDIT_VAR", "value": "1"})
    api_check("export_env_file (внутри)", "/api/export_env_file", {"folder": audit_dir, "content": "X=1"})
    api_check("export_env_file (снаружи)", "/api/export_env_file", {"folder": "C:\\Windows\\Temp",
                                                                   "content": "X=1"}, want=False)
    api_check("import_env_scan", "/api/import_env_scan", {"folder": audit_dir})
    api_check("import_env_scan (нет папки)", "/api/import_env_scan", {"folder": "Z:\\нет"}, want=False)
    api_check("import_env_scan (пусто)", "/api/import_env_scan", {"folder": ""}, want=False)
    api_check("import_env_save", "/api/import_env_save", {"items": [{"env_name": "AUDIT_IMPORTED",
                                                                    "value": "v1"}]})
    api_check("open_note", "/api/open_note", {"id": PFX + "p"})
    api_check("sync_note", "/api/sync_note", {"id": PFX + "p"})
    api_check("sync_note (нет файла)", "/api/sync_note", {"id": "нет-такого"}, want=False)

    st, txt = http("/api/save_key", raw="{это не json")
    (ok if st == 400 else fail)("битый JSON -> HTTP %s" % st)
    st, txt = http("/api/save_key", raw="[]")
    (ok if st in (200, 400) else fail)("JSON-массив -> HTTP %s" % st)
    st, _ = http("/api/nope", {"a": 1})
    (ok if st == 404 else fail)("POST неизвестного пути -> %s" % st)

    print("\n      очистка тестовых данных")
    for kid in (PFX + "k1", PFX + "k2", PFX + "k3"):
        http("/api/delete_key", {"id": kid})
    http("/api/delete_app", {"id": PFX + "app"})
    http("/api/delete_personal", {"id": PFX + "p"})
    st, txt = http("/api/get_data")
    data = json.loads(txt) if st == 200 else {"keys": []}
    for k in data.get("keys", []):
        if str(k.get("id", "")).startswith(PFX) or k.get("env_name") == "AUDIT_IMPORTED":
            http("/api/delete_key", {"id": k["id"]})
            ok("удалён забытый тестовый ключ %s" % k["id"])
    shutil.rmtree(audit_dir, ignore_errors=True)
    for f in os.listdir(os.path.join(ROOT, "notes")) if os.path.isdir(os.path.join(ROOT, "notes")) else []:
        if f.startswith(PFX):
            try:
                os.remove(os.path.join(ROOT, "notes", f))
            except OSError:
                pass


def cleanup_only():
    st, txt = http("/api/get_data")
    if st != 200:
        print("приложение не отвечает")
        return
    data = json.loads(txt)
    n = 0
    for k in data.get("keys", []):
        if str(k.get("id", "")).startswith(PFX) or str(k.get("env_name", "")).startswith("AUDIT"):
            http("/api/delete_key", {"id": k["id"]})
            n += 1
    for a in data.get("apps", []):
        if str(a.get("id", "")).startswith(PFX) or a.get("name") == "AuditAgent":
            http("/api/delete_app", {"id": a["id"]})
            n += 1
    for p in data.get("personal", []):
        if str(p.get("id", "")).startswith(PFX) or p.get("provider") == "Audit":
            http("/api/delete_personal", {"id": p["id"]})
            n += 1
    shutil.rmtree(os.path.join(ROOT, "_audit_tmp"), ignore_errors=True)
    print("удалено тестовых записей: %d" % n)


# --------------------------------------------------------------------------- concurrency
def check_concurrency():
    print("\n[5/5] Параллельность сервера")
    http("/api/save_key", {"id": PFX + "slow", "service": "SlowTest", "env_name": "SLOW_KEY",
                           "category": "ai", "value": "x" * 40,
                           "base_url": "http://10.255.255.1:9999/v1"})
    codes = []
    lock = threading.Lock()
    t0 = time.time()

    def worker():
        st, _ = http("/api/ping", {"id": PFX + "slow"})
        with lock:
            codes.append(st)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    t_get = time.time()
    st_get, _ = http("/api/get_data", timeout=15)
    get_ms = (time.time() - t_get) * 1000
    for t in threads:
        t.join()
    total = time.time() - t0
    http("/api/delete_key", {"id": PFX + "slow"})

    if all(c == 200 for c in codes) and st_get == 200 and get_ms < 3000 and total < 25:
        ok("8 медленных ping за %.1f с, GET во время них за %.0f мс" % (total, get_ms))
    else:
        fail("параллельность: ping %s за %.1f с, GET %s за %.0f мс" % (set(codes), total, st_get, get_ms))


def main():
    static_only = "--static" in sys.argv
    if "--cleanup" in sys.argv:
        cleanup_only()
        return 0
    print("=" * 66)
    print("ПРОВЕРКА CYBER-VAULT — %s" % ROOT)
    print("=" * 66)
    try:
        check_static()
        if static_only:
            print("\n(только статика — эндпоинты пропущены)")
        else:
            st, _ = http("/api/get_data", timeout=5)
            if st != 200:
                print("\n!! Приложение не отвечает на %s — запусти start.bat" % BASE)
                fail("нет связи с приложением")
            else:
                check_endpoints()
                check_concurrency()
    except Exception as e:
        import traceback
        fail("проверка упала с исключением: %r" % (e,))
        traceback.print_exc()
    print("\n" + "=" * 66)
    if failures:
        print("ПРОВАЛЕНО: %d" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("ВСЁ ЧИСТО")
    return 0


if __name__ == "__main__":
    sys.exit(main())
