import os
import sys
import re
import time
import json
import socket
import threading
import subprocess
import shutil
import urllib.request
import urllib.error
import urllib.parse
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_LIBS = os.path.join(BASE_DIR, "libs")
if os.path.isdir(LOCAL_LIBS) and LOCAL_LIBS not in sys.path:
    sys.path.insert(0, LOCAL_LIBS)

from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

try:
    import winreg
except ImportError:
    winreg = None

VAULT_FILE = os.path.join(BASE_DIR, "vault.json")
VAULT_LOCK = threading.RLock()
SERVER_URL = ""
TRAY_ICON = None

CLIPBOARD_MONITOR_ENABLED = True
DETECTED_KEY_QUEUE = []
IGNORED_TOKENS = set()
LAST_CLIP_TEXT = ""

def is_autostart_enabled() -> bool:
    if sys.platform != "win32" or not winreg: 
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, "CyberVault")
        winreg.CloseKey(key)
        return True
    except:
        return False

def set_autostart(enable: bool) -> bool:
    if sys.platform != "win32" or not winreg: 
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if enable:
            bat_path = os.path.join(BASE_DIR, "start.bat")
            winreg.SetValueEx(key, "CyberVault", 0, winreg.REG_SZ, f'"{bat_path}"')
        else:
            try:
                winreg.DeleteValue(key, "CyberVault")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print("Autostart error:", e)
        return False

API_PATTERNS = [
    {
        "name": "OpenRouter API",
        "regex": r"^sk-or-v1-[a-fA-F0-9]{64}$",
        "service": "OpenRouter",
        "env_name": "OPENROUTER_API_KEY",
        "category": "ai",
        "provider_url": "https://openrouter.ai/keys",
        "base_url": "https://openrouter.ai/api/v1"
    },
    {
        "name": "Google Gemini / Cloud API",
        "regex": r"^AIza[0-9A-Za-z\-_]{35}$",
        "service": "Google Gemini",
        "env_name": "GEMINI_API_KEY",
        "category": "ai",
        "provider_url": "https://aistudio.google.com/app/apikey",
        "base_url": "https://generativelanguage.googleapis.com/v1beta"
    },
    {
        "name": "Groq Cloud API",
        "regex": r"^gsk_[a-zA-Z0-9]{48,56}$",
        "service": "Groq Cloud",
        "env_name": "GROQ_API_KEY",
        "category": "ai",
        "provider_url": "https://console.groq.com/keys",
        "base_url": "https://api.groq.com/openai/v1"
    },
    {
        "name": "Telegram Bot Token",
        "regex": r"^\d{8,11}:AA[A-Za-z0-9_-]{33}$",
        "service": "Telegram Bot",
        "env_name": "TELEGRAM_BOT_TOKEN",
        "category": "bots",
        "provider_url": "https://t.me/BotFather",
        "base_url": "https://api.telegram.org"
    },
    {
        "name": "HuggingFace Token",
        "regex": r"^hf_[a-zA-Z0-9]{34}$",
        "service": "HuggingFace",
        "env_name": "HF_TOKEN",
        "category": "ai",
        "provider_url": "https://huggingface.co/settings/tokens",
        "base_url": "https://api-inference.huggingface.co"
    },
    {
        "name": "GitHub Personal Token",
        "regex": r"^(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82})$",
        "service": "GitHub API",
        "env_name": "GITHUB_TOKEN",
        "category": "cloud",
        "provider_url": "https://github.com/settings/tokens",
        "base_url": "https://api.github.com"
    },
    {
        "name": "Anthropic Claude API",
        "regex": r"^sk-ant-api03-[a-zA-Z0-9\-_]{80,100}$",
        "service": "Anthropic Claude",
        "env_name": "ANTHROPIC_API_KEY",
        "category": "ai",
        "provider_url": "https://console.anthropic.com/settings/keys",
        "base_url": "https://api.anthropic.com/v1"
    },
    {
        "name": "OpenAI / DeepSeek / Generic sk-",
        "regex": r"^sk-[a-zA-Z0-9\-_]{28,120}$",
        "service": "OpenAI / DeepSeek",
        "env_name": "OPENAI_API_KEY",
        "category": "ai",
        "provider_url": "https://platform.openai.com/api-keys",
        "base_url": "https://api.openai.com/v1"
    }
]


# ---------------- запуск агентов: поиск цели и иконки ----------------

SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", ".venv", "env", "lib", "libs",
    "licenses", "license", "models", "docs", "doc", "tests", "test", "src",
    "scripts", "script", "backup", "logs", "log", "locales", "resources",
    "assets", "share-target", "dist", "build", "include", "temp", "tmp",
    "site-packages", "server", "servers", "webui",
}

LAUNCHABLE = (".exe", ".bat", ".cmd", ".lnk")


def _similar_name(a, b):
    a = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _pick_launcher(names, folder):
    stem_folder = os.path.basename(folder.rstrip("\\/"))

    def score(name):
        stem = os.path.splitext(name)[0]
        ext = os.path.splitext(name)[1].lower()
        s = 0
        if ext == ".exe":
            s -= 5
        elif ext == ".lnk":
            s -= 1
        if _similar_name(stem, stem_folder):
            s -= 10
        if re.search(r"(?i)(start|run|launch|main)", stem):
            s -= 3
        return s

    return sorted(names, key=lambda n: (score(n), n))[0]


def _find_launcher_in_folder(folder, depth=1):
    try:
        entries = [e for e in os.listdir(folder) if not e.startswith('.')]
    except OSError:
        return None
    launchers = [e for e in entries if e.lower().endswith(LAUNCHABLE)]
    if launchers:
        return os.path.join(folder, _pick_launcher(launchers, folder))
    if depth <= 0:
        return None
    found = []
    for sub in entries:
        sub_path = os.path.join(folder, sub)
        if not os.path.isdir(sub_path) or sub.lower() in SKIP_DIRS:
            continue
        hit = _find_launcher_in_folder(sub_path, depth - 1)
        if hit:
            found.append((sub, hit))
    if not found:
        return None
    parent = os.path.basename(folder.rstrip("\\/"))
    found.sort(key=lambda t: (0 if _similar_name(t[0], parent) else 1, t[0]))
    return found[0][1]


def resolve_launch_target(path):
    """Возвращает (kind, target): kind = file | dir | cmd | empty."""
    p = (path or "").strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1].strip()
    if not p:
        return "empty", ""
    if os.path.isfile(p):
        return "file", p
    if os.path.isdir(p):
        launcher = _find_launcher_in_folder(p)
        if launcher:
            return "file", launcher
        return "dir", p
    return "cmd", p


def extract_exe_icon_png(exe_path):
    """Достаёт иконку .exe и возвращает data:image/png;base64,... либо None."""
    try:
        import base64
        import ctypes
        from ctypes import wintypes
        from io import BytesIO
        from PIL import Image

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                        ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                        ("bmPlanes", wintypes.WORD), ("bmBitCount", wintypes.WORD),
                        ("bmBits", wintypes.LPVOID)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                           ctypes.POINTER(wintypes.HICON),
                                           ctypes.POINTER(wintypes.HICON), wintypes.UINT]
        shell32.ExtractIconExW.restype = wintypes.DWORD
        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = wintypes.DWORD
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int

        large = wintypes.HICON()
        small = wintypes.HICON()
        shell32.ExtractIconExW(exe_path, 0, ctypes.byref(large), ctypes.byref(small), 1)
        hicon = large if large.value else small
        if not hicon:
            return None
        info = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(info)):
            return None
        hbm = info.hbmColor if info.hbmColor else info.hbmMask
        if not hbm:
            return None
        bmp = BITMAP()
        if not gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), ctypes.byref(bmp)):
            return None
        w, h = int(bmp.bmWidth), int(bmp.bmHeight)
        if h == 2 * w:
            h = w
        if w <= 0 or h <= 0:
            return None
        hdc = gdi32.CreateCompatibleDC(user32.GetDC(None))
        if not hdc:
            return None
        old = gdi32.SelectObject(hdc, hbm)
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = w
        header.biHeight = -h
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(header), 0)
        try:
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteDC(hdc)
        except Exception:
            pass
        if got == 0:
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        out = BytesIO()
        img.save(out, format="PNG")
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        try:
            import traceback
            with open(os.path.join(BASE_DIR, "icon_debug.log"), "a", encoding="utf-8") as _f:
                _f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        return None


def agent_icon_data_url(path):
    kind, target = resolve_launch_target(path)
    if kind != "file" or not target.lower().endswith(".exe"):
        return None
    return extract_exe_icon_png(target)


# ---------------- провайдеры: основной ключ, ротация, бекапы ----------------

def provider_signature(k):
    return (k.get("service") or "").strip().lower() + "|" + (k.get("category") or "").strip().lower()


def provider_groups(keys):
    groups = {}
    for k in keys:
        groups.setdefault(provider_signature(k), []).append(k)
    return groups


def base_env_name(env_name):
    return re.sub(r"(_API_KEY|_KEY|_TOKEN)$", "_BASE_URL", env_name or "")


def resolve_provider_env(keys):
    """{env_name: value} по набору ключей: основной ключ либо склейка при ротации.

    Имя переменной берётся у первого ключа группы (каноническое), значение —
    у основного ключа (или склейка через запятую при ротации).
    """
    out = {}
    for sig, group in provider_groups(keys).items():
        canonical = group[0]
        primary = next((k for k in group if k.get("primary")), canonical)
        rotate = any(k.get("rotation") for k in group) and len(group) > 1
        values = [primary.get("value") or ""]
        if rotate:
            values += [(k.get("value") or "") for k in group if k is not primary]
        value = ",".join(v for v in values if v)
        env_name = canonical.get("env_name") or ""
        if not env_name:
            continue
        out[env_name] = value
        if canonical.get("base_url"):
            out[base_env_name(env_name)] = canonical.get("base_url")
    return out


def backup_vault(keep=20):
    """Копия vault.json в backup/ после каждого сохранения."""
    try:
        bdir = os.path.join(BASE_DIR, "backup")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(bdir, "vault_%s.json" % stamp)
        shutil.copy2(VAULT_FILE, dst)
        files = []
        for name in os.listdir(bdir):
            if name.startswith("vault_") and name.endswith(".json"):
                path = os.path.join(bdir, name)
                try:
                    files.append((os.path.getmtime(path), path))
                except OSError:
                    pass
        files.sort(reverse=True)
        for _, old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass


def detect_service_for_env(env_name):
    for pat in API_PATTERNS:
        if pat.get("env_name") == env_name:
            return {
                "service": pat.get("name", ""),
                "category": pat.get("category", "ai"),
                "provider_url": pat.get("provider_url", ""),
                "base_url": pat.get("base_url", ""),
            }
    return {"service": "", "category": "ai", "provider_url": "", "base_url": ""}


def scan_env_folder(folder):
    """Ищет .env файлы и возвращает список ключей, найденных в них."""
    skip_dirs = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist",
                 "build", "models", "lib", "site-packages", ".cache", "target"}
    found = []
    folder = os.path.abspath(folder)
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs and not d.startswith('.')]
        rel = os.path.relpath(root, folder)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= 3:
            dirs[:] = []
            continue
        for name in files:
            low = name.lower()
            if not (low == ".env" or low.endswith(".env") or low.startswith(".env.")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            for num, raw in enumerate(content.splitlines(), 1):
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name_part, _, value = line.partition("=")
                name_part = name_part.strip()
                value = value.strip().strip('"').strip("'")
                if not name_part or not value:
                    continue
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name_part):
                    continue
                found.append({
                    "file": os.path.relpath(path, folder),
                    "line": num,
                    "env_name": name_part,
                    "value": value,
                    "hint": detect_service_for_env(name_part),
                })
                if len(found) >= 500:
                    return found
    return found


class Vault:
    def __init__(self):
        self.data = {"keys": [], "apps": [], "personal": []}
        self.load()

    def load(self):
        if os.path.exists(VAULT_FILE):
            try:
                with open(VAULT_FILE, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                
                self.data.setdefault("keys", [])
                self.data.setdefault("apps", [])
                self.data.setdefault("personal", [])
                
                for k in self.data["keys"]:
                    k.setdefault("provider_url", "")
                    k.setdefault("base_url", "")
                
                if not self.data["apps"]:
                    self.data["apps"] = [
                        {
                            "id": "app_vizor",
                            "name": "My Agent",
                            "path": "C:\\Apps\\my-agent",
                            "icon": "🤖",
                            "linked_keys": []
                        },
                        {
                            "id": "app_scrininfo",
                            "name": "OCR Service",
                            "path": "C:\\Apps\\ocr-service",
                            "icon": "📸",
                            "linked_keys": []
                        },
                        {
                            "id": "app_ollama",
                            "name": "Ollama Portable",
                            "path": "C:\\Apps\\ollama-portable",
                            "icon": "🦙",
                            "linked_keys": []
                        },
                        {
                            "id": "app_n8n",
                            "name": "n8n Orders",
                            "path": "n8n start",
                            "icon": "⚙️",
                            "linked_keys": []
                        }
                    ]
                    self.save()
                return
            except Exception as e:
                print(f"Error loading vault.json: {e}")
        
        self.data = {
            "keys": [],
            "apps": [],
            "personal": []
        }
        self.save()

    def save(self):
        with VAULT_LOCK:
            tmp = "%s.%d.tmp" % (VAULT_FILE, threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, VAULT_FILE)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        backup_vault()


VAULT = Vault()


def parse_clipboard_candidate(raw_text: str):
    if not raw_text:
        return None
    text = raw_text.strip()
    if len(text) < 12 or len(text) > 300 or "\n" in text:
        return None

    env_match = re.match(r'^([A-Z0-9_]{3,45})\s*=\s*["\']?([A-Za-z0-9\-_:\.]{12,200})["\']?$', text)
    if env_match:
        env_var, token_val = env_match.group(1), env_match.group(2)
        for pat in API_PATTERNS:
            if re.match(pat["regex"], token_val):
                return {
                    "service": pat["service"],
                    "env_name": env_var,
                    "value": token_val,
                    "category": pat["category"],
                    "provider_url": pat["provider_url"],
                    "base_url": pat["base_url"],
                    "detected_type": f"{pat['name']} (.env формат)"
                }
        return {
            "service": env_var.replace("_API_KEY", "").replace("_TOKEN", "").title(),
            "env_name": env_var,
            "value": token_val,
            "category": "ai",
            "provider_url": "",
            "base_url": "",
            "detected_type": "ENV переменная"
        }

    for pat in API_PATTERNS:
        if re.match(pat["regex"], text):
            return {
                "service": pat["service"],
                "env_name": pat["env_name"],
                "value": text,
                "category": pat["category"],
                "provider_url": pat["provider_url"],
                "base_url": pat["base_url"],
                "detected_type": pat["name"]
            }
    return None


def clipboard_watcher_loop():
    global LAST_CLIP_TEXT
    while True:
        time.sleep(1.2)
        if not CLIPBOARD_MONITOR_ENABLED or not pyperclip:
            continue
        try:
            clip = pyperclip.paste()
            if not clip or clip == LAST_CLIP_TEXT:
                continue
            LAST_CLIP_TEXT = clip

            candidate = parse_clipboard_candidate(clip)
            if not candidate:
                continue

            token_val = candidate["value"]
            if token_val in IGNORED_TOKENS:
                continue

            existing_values = {k.get("value") for k in VAULT.data.get("keys", [])}
            if token_val in existing_values:
                continue

            IGNORED_TOKENS.add(token_val)
            DETECTED_KEY_QUEUE.append(candidate)

            if TRAY_ICON:
                try:
                    TRAY_ICON.notify(
                        f"Обнаружен ключ: {candidate['detected_type']}\nОткройте окно Cyber-Vault для сохранения.",
                        "CYBER-VAULT // Перехват ключа"
                    )
                except Exception:
                    pass
        except Exception:
            pass


def check_key_health(key_obj: dict) -> dict:
    service = key_obj.get("service", "")
    env_name = key_obj.get("env_name", "")
    value = key_obj.get("value", "").strip()
    base_url = key_obj.get("base_url", "").strip()

    if not value or "demo" in value.lower():
        return {"ok": False, "msg": "Демо-ключ (замените на реальный)"}

    s = (service + " " + env_name + " " + base_url).lower()
    try:
        if "openrouter.ai" in s or "openrouter" in s:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {value}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                info = json.loads(resp.read().decode("utf-8")).get("data", {})
                usage = info.get("usage", 0)
                limit = info.get("limit")
                limit_str = f"${limit:.2f}" if limit else "Безлимит"
                return {"ok": True, "msg": f"Рабочий | Потрачено: ${usage:.3f} / Лимит: {limit_str}"}

        elif "telegram" in s:
            api_root = base_url.rstrip("/") if base_url else "https://api.telegram.org"
            req = urllib.request.Request(f"{api_root}/bot{value}/getMe")
            with urllib.request.urlopen(req, timeout=5) as resp:
                info = json.loads(resp.read().decode("utf-8"))
                if info.get("ok"):
                    bot_name = info["result"].get("username", "Bot")
                    return {"ok": True, "msg": f"Рабочий | Бот @{bot_name}"}

        elif "gemini" in s or "generativelanguage" in s:
            api_root = base_url.rstrip("/") if base_url else "https://generativelanguage.googleapis.com/v1beta"
            url = f"{api_root}/models?key={value}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return {"ok": True, "msg": "Рабочий | Gemini API (200 OK)"}

        elif base_url:
            clean_base = base_url.rstrip("/")
            test_url = clean_base if clean_base.endswith("/models") else f"{clean_base}/models"
            req = urllib.request.Request(
                test_url,
                headers={"Authorization": f"Bearer {value}", "User-Agent": "CyberVault/1.5"}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return {"ok": True, "msg": f"Рабочий | HTTP {resp.status} ({clean_base})"}
            except urllib.error.HTTPError as he:
                if he.code in (401, 403):
                    return {"ok": False, "msg": f"Неверный ключ | HTTP {he.code}"}
                req2 = urllib.request.Request(
                    clean_base,
                    headers={"Authorization": f"Bearer {value}", "User-Agent": "CyberVault/1.5"}
                )
                with urllib.request.urlopen(req2, timeout=5) as resp2:
                    return {"ok": True, "msg": f"Узел доступен | HTTP {resp2.status}"}

        if len(value) >= 8:
            return {"ok": True, "msg": "Формат корректен (локально)"}
        return {"ok": False, "msg": "Слишком короткий токен"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "msg": f"Нерабочий | HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": f"Ошибка соединения: {str(e)[:35]}"}


COLOR_ICON_SVG_DATA = "data:image/svg+xml;utf8," + urllib.parse.quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect x="4" y="4" width="56" height="56" rx="14" fill="#0c1317" stroke="#355C69" stroke-width="3"/>'
    '<path d="M4 18 V8 Q4 4 8 4 H18" fill="none" stroke="#f59e0b" stroke-width="3.5"/>'
    '<path d="M46 4 H56 Q60 4 60 8 V18" fill="none" stroke="#f59e0b" stroke-width="3.5"/>'
    '<path d="M4 46 V56 Q4 60 8 60 H18" fill="none" stroke="#f59e0b" stroke-width="3.5"/>'
    '<path d="M46 60 H56 Q60 60 60 56 V46" fill="none" stroke="#f59e0b" stroke-width="3.5"/>'
    '<polygon points="32,12 50,32 32,52 14,32" fill="#355C69" stroke="#22d3ee" stroke-width="2"/>'
    '<polygon points="32,21 41,32 32,43 23,32" fill="#f59e0b"/>'
    '</svg>'
)


HTML_UI = r"""<!DOCTYPE html>
<html lang="ru" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CYBER-VAULT // API Key Manager</title>
<link rel="icon" type="image/svg+xml" href="__FAVICON_DATA__">
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --amber: #f59e0b;
  }
  body {
    background-color: #050505;
    color: #f4f4f5;
    font-family: 'Inter', sans-serif;
    background-image:
      radial-gradient(circle at 15% 15%, rgba(245, 158, 11, 0.08) 0%, transparent 35%),
      radial-gradient(circle at 85% 85%, rgba(217, 119, 6, 0.06) 0%, transparent 40%),
      linear-gradient(to bottom, #050505, #09090b);
    min-height: 100vh;
    overflow-x: hidden;
  }
  .mono { font-family: 'JetBrains Mono', monospace; }

  .hud-card {
    position: relative;
    background: rgba(24, 24, 27, 0.45);
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    transition: all 0.25s ease;
  }
  .hud-card:hover {
    border-color: rgba(245, 158, 11, 0.35);
    box-shadow: 0 0 28px rgba(245, 158, 11, 0.08);
  }
  .hud-corner-tl, .hud-corner-tr, .hud-corner-bl, .hud-corner-br {
    position: absolute;
    width: 10px;
    height: 10px;
    pointer-events: none;
    transition: all 0.25s ease;
  }
  .hud-corner-tl { top: -1px; left: -1px; border-top: 2px solid #f59e0b; border-left: 2px solid #f59e0b; border-top-left-radius: 6px; }
  .hud-corner-tr { top: -1px; right: -1px; border-top: 2px solid #f59e0b; border-right: 2px solid #f59e0b; border-top-right-radius: 6px; }
  .hud-corner-bl { bottom: -1px; left: -1px; border-bottom: 2px solid #f59e0b; border-left: 2px solid #f59e0b; border-bottom-left-radius: 6px; }
  .hud-corner-br { bottom: -1px; right: -1px; border-bottom: 2px solid #f59e0b; border-right: 2px solid #f59e0b; border-bottom-right-radius: 6px; }

  .amber-cta {
    background-color: #355C69;
    color: #ffffff;
    font-weight: 600;
    border: 1px solid rgba(255, 255, 255, 0.12);
    box-shadow: 0 2px 10px rgba(53, 92, 105, 0.35);
    transition: all 0.2s ease;
    cursor: pointer;
  }
  .amber-cta:hover {
    background-color: #3f6d7c;
    box-shadow: 0 4px 16px rgba(53, 92, 105, 0.55);
    transform: translateY(-1px);
  }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: #09090b; }
  ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #f59e0b; }
</style>
</head>
<body class="select-none">

<div id="app-screen" class="h-screen flex flex-col">
  <header class="relative z-40 h-16 border-b border-white/10 bg-zinc-950/80 backdrop-blur-xl px-6 flex items-center justify-between shrink-0">
    <div class="flex items-center gap-3.5">
      <div class="w-10 h-10 rounded-xl bg-[#0c1317] border border-amber-400/50 flex items-center justify-center shadow-[0_0_15px_rgba(34,211,238,0.2)]">
        <svg class="w-6 h-6" viewBox="0 0 64 64">
          <polygon points="32,8 54,32 32,56 10,32" fill="#355C69" stroke="#22d3ee" stroke-width="2.5"/>
          <polygon points="32,19 43,32 32,45 21,32" fill="#f59e0b"/>
        </svg>
      </div>
      <div>
        <div class="flex items-center gap-2">
          <span class="font-bold tracking-wide text-white">CYBER<span class="text-amber-400">VAULT</span></span>
          <span class="text-[10px] mono px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/30">HUD v1.6</span>
        </div>
        <div class="text-[11px] text-zinc-500 mono">Локальный менеджер API ключей (JSON)</div>
      </div>
    </div>

    <div class="flex-1 max-w-md mx-4">
      <div class="relative">
        <input id="search-input" type="text" oninput="renderCurrentView()"
          placeholder="Фильтр по сервису, ENV, URL или агенту..."
          class="w-full pl-9 pr-4 py-2 rounded-xl bg-zinc-900/70 border border-zinc-800 text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-amber-400/60 transition-all" />
        <span class="absolute left-3 top-2.5 text-zinc-500 text-xs mono">⌕</span>
      </div>
    </div>

    <div class="flex items-center gap-2">
      <div id="clipboard-timer" class="hidden items-center gap-2 px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs mono">
        <span>⏳ <b id="timer-sec">15</b>с</span>
      </div>

      <button onclick="openImportModal()" class="px-3 py-2 rounded-xl bg-zinc-900/80 hover:bg-zinc-800 border border-amber-500/40 text-xs text-amber-300 mono flex items-center gap-1.5 transition-all" title="Импортировать ключи из .env файлов проекта">
        <span>📂</span> Импорт .env
      </button>

      <button onclick="grabClipboardNow()" class="px-3 py-2 rounded-xl bg-zinc-900/80 hover:bg-zinc-800 border border-cyan-500/40 text-xs text-cyan-300 mono flex items-center gap-1.5 transition-all" title="Распознать и добавить скопированный ключ из буфера обмена">
        <span>📥</span> Из буфера
      </button>

      <button id="toggle-view-btn" onclick="toggleViewMode()" class="px-3.5 py-2 rounded-xl amber-cta text-xs mono flex items-center gap-1.5">
        <span>☰</span> Ключи списком
      </button>

      <button onclick="pingAllKeys()" class="px-3 py-2 rounded-xl bg-zinc-900/60 hover:bg-zinc-800 border border-zinc-700/60 text-xs text-zinc-300 mono flex items-center gap-1.5 transition-all">
        <span class="text-amber-400">⚡</span> Ping
      </button>

      <div class="relative">
        <button id="quick-launch-btn" onclick="toggleQuickLaunch()" class="px-3.5 py-2 rounded-xl bg-cyan-950/60 hover:bg-cyan-900/60 border border-cyan-700/50 text-xs mono flex items-center gap-1.5 text-cyan-300 transition-all" title="Быстрый запуск агента">
          <span>▶</span> Запустить
        </button>
        <div id="quick-launch-menu" class="hidden absolute right-0 top-full mt-2 z-50 w-64 rounded-xl border border-cyan-700/40 bg-zinc-950/95 backdrop-blur p-2 shadow-2xl"></div>
      </div>

      <button id="main-add-btn" onclick="openModal()" class="px-4 py-2 rounded-xl amber-cta text-xs uppercase tracking-wider mono flex items-center gap-1.5">
        <span>+</span> Новый ключ
      </button>
    </div>
  </header>

  <div id="intercept-banner" class="hidden bg-[#101f24] border-b border-cyan-500/40 px-6 py-3 flex items-center justify-between shadow-lg">
    <div class="flex items-center gap-3">
      <span class="w-2.5 h-2.5 rounded-full bg-cyan-400 animate-ping"></span>
      <div class="text-xs mono">
        <span class="text-cyan-300 font-bold">[ПЕРЕХВАТ БУФЕРА ОБМЕНА]:</span>
        <span id="intercept-desc" class="text-zinc-200 ml-1">Обнаружен новый API-ключ</span>
      </div>
    </div>
    <div class="flex items-center gap-2">
      <button onclick="acceptInterceptedKey()" class="px-3.5 py-1.5 rounded-lg amber-cta text-xs mono">
        ✓ Внести в базу
      </button>
      <button onclick="dismissInterceptedKey()" class="px-3 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-xs text-zinc-400 mono">
        Игнорировать
      </button>
    </div>
  </div>

  <div class="flex-1 flex overflow-hidden">
    <aside class="w-64 border-r border-white/10 bg-zinc-950/50 p-4 flex flex-col justify-between shrink-0">
      <div class="space-y-1.5">
        <div class="text-[10px] uppercase tracking-widest text-zinc-500 mono px-3 mb-2">Режим отображения</div>
        
        <button onclick="setCategory('all')" id="cat-all" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all">
          <span>🔑 Карточки (Все)</span>
          <span id="count-all" class="text-xs mono px-2 py-0.5 rounded-md bg-zinc-800 text-zinc-300">0</span>
        </button>
        <button onclick="setCategory('list_view')" id="cat-list_view" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all">
          <span>📋 Ключи списком</span>
          <span class="text-[10px] mono px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400">СТАТУС</span>
        </button>

        <!-- Вкладка Агенты ИИ -->
        <button onclick="setCategory('agents')" id="cat-agents" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all border border-transparent hover:bg-cyan-900/20 text-cyan-400 mt-1">
          <span>🤖 Агенты ИИ</span>
          <span id="count-agents" class="text-xs mono px-2 py-0.5 rounded-md bg-cyan-950 text-cyan-400">0</span>
        </button>

        <!-- Вкладка Личные ключи -->
        <button onclick="setCategory('personal')" id="cat-personal" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all border border-transparent hover:bg-emerald-900/20 text-emerald-400 mt-1">
          <span>🔐 Личные ключи</span>
          <span id="count-personal" class="text-xs mono px-2 py-0.5 rounded-md bg-emerald-950 text-emerald-400">0</span>
        </button>

        <div class="pt-3 mt-3 border-t border-white/10">
          <div class="text-[10px] uppercase tracking-widest text-zinc-500 mono px-3 mb-2">Фильтр API по группам</div>
          <button onclick="setCategory('ai')" id="cat-ai" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all">
            <span>🧠 AI & LLM</span>
            <span id="count-ai" class="text-xs mono px-2 py-0.5 rounded-md bg-zinc-800 text-zinc-300">0</span>
          </button>
          <button onclick="setCategory('bots')" id="cat-bots" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all">
            <span>💬 Боты и Соцсети</span>
            <span id="count-bots" class="text-xs mono px-2 py-0.5 rounded-md bg-zinc-800 text-zinc-300">0</span>
          </button>
          <button onclick="setCategory('cloud')" id="cat-cloud" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all">
            <span>☁️ Облака и OCR</span>
            <span id="count-cloud" class="text-xs mono px-2 py-0.5 rounded-md bg-zinc-800 text-zinc-300">0</span>
          </button>
        </div>

        <div class="pt-3 mt-3 border-t border-white/10">
          <div class="text-[10px] uppercase tracking-widest text-zinc-500 mono px-3 mb-2">Инструменты</div>
          <button onclick="setCategory('env_builder')" id="cat-env_builder" class="cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all">
            <span>📦 Сборщик .env</span>
            <span class="text-[10px] mono text-amber-400">GEN</span>
          </button>
        </div>
      </div>

      <div class="space-y-2.5 pt-4 border-t border-white/10">
        <div class="p-3 rounded-xl bg-zinc-900/60 border border-zinc-800 text-[11px] text-zinc-400 mono space-y-2.5">
          <div class="flex items-center justify-between">
            <span>Перехват буфера:</span>
            <input type="checkbox" id="clip-monitor-toggle" checked onchange="toggleClipMonitor()" class="accent-[#355C69] w-4 h-4 cursor-pointer" />
          </div>
          <div class="flex items-center justify-between">
            <span>Запуск с Windows:</span>
            <input type="checkbox" id="autostart-toggle" onchange="toggleAutostart()" class="accent-emerald-600 w-4 h-4 cursor-pointer" />
          </div>
          <div class="flex justify-between pt-1 border-t border-zinc-800/80"><span>Контейнер:</span><span class="text-zinc-300">vault.json</span></div>
        </div>
        <button onclick="window.close()" class="w-full py-2.5 rounded-xl bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 hover:border-zinc-700 text-xs text-zinc-300 mono transition-all">
          ↘ Свернуть в трей
        </button>
      </div>
    </aside>

    <main class="flex-1 overflow-y-auto p-6 relative">
      <div id="keys-view" class="grid grid-cols-1 xl:grid-cols-2 gap-4 auto-rows-fr"></div>

      <!-- ВЬЮХА АГЕНТОВ ИИ -->
      <div id="agents-view" class="hidden grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5 auto-rows-fr"></div>
      
      <!-- ВЬЮХА ЛИЧНЫХ КЛЮЧЕЙ -->
      <div id="personal-view" class="hidden grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5 auto-rows-fr"></div>

      <div id="list-view" class="hidden">
        <div class="hud-card rounded-2xl overflow-hidden">
          <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span>
          <span class="hud-corner-bl"></span><span class="hud-corner-br"></span>

          <div class="px-5 py-4 border-b border-white/10 flex items-center justify-between bg-zinc-900/40">
            <div>
              <h2 class="text-sm font-bold text-white uppercase tracking-wider mono">Реестр API-ключей</h2>
              <p class="text-xs text-zinc-400 mt-0.5">Клик по имени провайдера открывает его сайт.</p>
            </div>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left border-collapse">
              <thead>
                <tr class="border-b border-white/10 text-[11px] uppercase tracking-wider text-zinc-400 mono bg-black/30">
                  <th class="py-3 px-4">Провайдер (Сайт)</th>
                  <th class="py-3 px-4">Переменная ENV / Base HTTP</th>
                  <th class="py-3 px-4">Токен</th>
                  <th class="py-3 px-4">Статус (Рабочий / Нет)</th>
                  <th class="py-3 px-4 text-right">Действия</th>
                </tr>
              </thead>
              <tbody id="list-table-body" class="divide-y divide-white/5 text-sm"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div id="env-builder-view" class="hidden max-w-3xl mx-auto space-y-6">
        <div class="hud-card rounded-2xl p-6">
          <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span>
          <span class="hud-corner-bl"></span><span class="hud-corner-br"></span>
          <h2 class="text-lg font-bold text-white mb-1">Конструктор файла <span class="text-amber-400 mono">.env</span></h2>
          <p class="text-xs text-zinc-400 mb-4">Отметьте нужные ключи галочками, чтобы сгенерировать готовый конфиг.</p>

          <div id="env-checkbox-list" class="space-y-2 mb-5 max-h-60 overflow-y-auto pr-2"></div>

          <div class="space-y-2">
            <label class="text-xs uppercase tracking-wider text-zinc-400 mono">Предпросмотр содержимого .env</label>
            <textarea id="env-preview" readonly rows="8"
              class="w-full p-4 rounded-xl bg-black/70 border border-zinc-800 text-amber-300 mono text-xs focus:outline-none"></textarea>
          </div>

          <div class="mt-4 flex flex-wrap items-center gap-3">
            <button onclick="copyGeneratedEnv()" class="px-5 py-2.5 rounded-xl amber-cta text-xs mono uppercase">
              📋 Скопировать весь .env
            </button>
            <div class="flex-1 flex items-center gap-2">
              <input id="export-dir-path" type="text" placeholder="Путь к папке проекта (пусто = текущая папка)"
                class="flex-1 px-3.5 py-2 rounded-xl bg-zinc-900/70 border border-zinc-800 text-xs text-zinc-200 mono focus:outline-none focus:border-amber-400/60" />
              <button onclick="saveEnvToFile()" class="px-4 py-2 rounded-xl amber-cta text-xs mono transition-all">
                💾 Сохранить .env на диск
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</div>

<!-- Модальное окно импорта .env -->
<div id="import-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
  <div class="hud-card w-full max-w-2xl rounded-2xl p-6 relative">
    <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span>
    <span class="hud-corner-bl"></span><span class="hud-corner-br"></span>

    <div class="flex items-center justify-between mb-5">
      <h3 class="text-base font-bold text-white uppercase tracking-wider mono">
        📂 Импорт <span class="text-amber-400">.env</span>
      </h3>
      <button onclick="closeModal('import-modal')" class="text-zinc-500 hover:text-white text-lg">✕</button>
    </div>

    <div class="flex gap-2 items-end mb-4">
      <div class="flex-1">
        <label class="block text-xs text-zinc-400 mb-1 mono">Папка проекта</label>
        <input id="import-folder" placeholder="C:\Apps\my-project" onkeydown="if(event.key==='Enter'){event.preventDefault();scanEnvFiles();}"
          class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-amber-300 mono focus:border-amber-400/60 focus:outline-none" />
      </div>
      <button onclick="scanEnvFiles()" class="px-4 py-2 rounded-xl bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-xs text-zinc-200 mono">
        🔍 Сканировать
      </button>
    </div>

    <div id="import-results" class="max-h-72 overflow-y-auto space-y-1 pr-1 mb-5"></div>

    <div class="flex justify-end gap-2">
      <button onclick="closeModal('import-modal')" class="px-4 py-2 rounded-xl bg-zinc-900 border border-zinc-700 text-xs text-zinc-300 mono">Отмена</button>
      <button onclick="importSelectedKeys()" class="px-4 py-2 rounded-xl amber-cta text-xs text-white mono">
        ⬇ Импортировать выбранные
      </button>
    </div>
  </div>
</div>

<!-- Модальное окно API Ключа -->
<div id="key-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
  <div class="hud-card w-full max-w-xl rounded-2xl p-6 relative">
    <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span>
    <span class="hud-corner-bl"></span><span class="hud-corner-br"></span>

    <div class="flex items-center justify-between mb-5">
      <h3 id="modal-title" class="text-base font-bold text-white uppercase tracking-wider mono">
        + Добавить <span class="text-amber-400">API Ключ</span>
      </h3>
      <button onclick="closeModal('key-modal')" class="text-zinc-500 hover:text-white text-lg">✕</button>
    </div>

    <form onsubmit="saveKeyForm(event)" class="space-y-3.5">
      <input type="hidden" id="form-id" />
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Имя провайдера / Сервиса *</label>
          <input id="form-service" required placeholder="OpenRouter / DeepSeek / Local"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-white focus:border-amber-400/60 focus:outline-none" />
        </div>
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Категория</label>
          <select id="form-category"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-white focus:border-amber-400/60 focus:outline-none">
            <option value="ai">🤖 AI & LLM</option>
            <option value="bots">💬 Боты и Соцсети</option>
            <option value="cloud">☁️ Облака и OCR</option>
          </select>
        </div>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Сайт провайдера (для клика)</label>
          <input id="form-provider-url" placeholder="https://openrouter.ai/keys"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-xs text-zinc-200 mono focus:border-amber-400/60 focus:outline-none" />
        </div>
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Базовый HTTP URL</label>
          <input id="form-base-url" placeholder="http://127.0.0.1:11434/v1"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-xs text-amber-300 mono focus:border-amber-400/60 focus:outline-none" />
        </div>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Имя переменной (ENV) *</label>
          <input id="form-env" required placeholder="OPENROUTER_API_KEY"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-amber-300 mono focus:border-amber-400/60 focus:outline-none" />
        </div>
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Тег проекта</label>
          <input id="form-project" placeholder="VIZOR / Scrininfo"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-white focus:border-amber-400/60 focus:outline-none" />
        </div>
      </div>
      <div>
        <label class="block text-xs text-zinc-400 mb-1 mono">Секретный токен / API-ключ *</label>
        <input id="form-value" required placeholder="sk-..."
          class="w-full px-3.5 py-2.5 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-emerald-300 mono focus:border-amber-400/60 focus:outline-none" />
      </div>
      <div>
        <label class="block text-xs text-zinc-400 mb-1 mono">Заметка (тариф, лимиты)</label>
        <input id="form-notes" placeholder="Основной ключ..."
          class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-zinc-300 focus:border-amber-400/60 focus:outline-none" />
      </div>
      <div class="flex justify-end gap-3 pt-3">
        <button type="button" onclick="closeModal('key-modal')" class="px-4 py-2 rounded-xl bg-zinc-900 border border-zinc-700 text-xs text-zinc-300 mono">Отмена</button>
        <button type="submit" class="px-5 py-2 rounded-xl amber-cta text-xs uppercase mono">Сохранить</button>
      </div>
    </form>
  </div>
</div>

<!-- Модальное окно Агента -->
<div id="agent-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
  <div class="hud-card w-full max-w-lg rounded-2xl p-6 relative">
    <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span><span class="hud-corner-bl"></span><span class="hud-corner-br"></span>
    <div class="flex items-center justify-between mb-5">
      <h3 id="agent-modal-title" class="text-base font-bold text-white uppercase tracking-wider mono">
        + Добавить <span class="text-cyan-400">Агента</span>
      </h3>
      <button onclick="closeModal('agent-modal')" class="text-zinc-500 hover:text-white text-lg">✕</button>
    </div>

    <form onsubmit="saveAgentForm(event)" class="space-y-4">
      <input type="hidden" id="agent-form-id" />
      <div class="grid grid-cols-[3fr_1fr] gap-3">
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Название Агента *</label>
          <input id="agent-form-name" required placeholder="VIZOR / Ollama / Cline"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-white focus:border-cyan-400/60 focus:outline-none" />
        </div>
        <div>
          <label class="block text-xs text-zinc-400 mb-1 mono">Иконка</label>
          <input id="agent-form-icon" placeholder="🤖" value="🤖"
            class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-lg text-center focus:border-cyan-400/60 focus:outline-none" />
        </div>
      </div>
      <div>
        <label class="block text-xs text-zinc-400 mb-1 mono">Путь запуска (папка, .exe или команда) *</label>
        <input id="agent-form-path" required placeholder="C:\Apps\my-agent.exe  или  n8n start"
          class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-zinc-200 mono focus:border-cyan-400/60 focus:outline-none" />
        <p class="text-[10px] text-zinc-500 mt-1.5 leading-tight">Если указать папку, она откроется в проводнике. Если указать .exe или .bat, он запустится.</p>
      </div>

      <div>
        <label class="block text-xs text-zinc-400 mb-2 mono">Связанные API-ключи (для проверки конфликтов):</label>
        <div id="agent-keys-checkboxes" class="max-h-40 overflow-y-auto space-y-1.5 p-2 bg-zinc-900/50 rounded-xl border border-zinc-800/80">
          <!-- Чекбоксы ключей рендерятся тут -->
        </div>
      </div>

      <div class="flex justify-end gap-3 pt-3">
        <button type="button" onclick="closeModal('agent-modal')" class="px-4 py-2 rounded-xl bg-zinc-900 border border-zinc-700 text-xs text-zinc-300 mono">Отмена</button>
        <button type="submit" class="px-5 py-2 rounded-xl bg-cyan-600 hover:bg-cyan-500 text-white font-bold shadow-[0_0_15px_rgba(8,145,178,0.4)] text-xs uppercase mono">Сохранить</button>
      </div>
    </form>
  </div>
</div>

<!-- Модальное окно Личного ключа -->
<div id="personal-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md">
  <div class="hud-card w-full max-w-md rounded-2xl p-6 relative">
    <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span><span class="hud-corner-bl"></span><span class="hud-corner-br"></span>
    <div class="flex items-center justify-between mb-5">
      <h3 id="personal-modal-title" class="text-base font-bold text-white uppercase tracking-wider mono">
        + Добавить <span class="text-emerald-400">Личный ключ</span>
      </h3>
      <button onclick="closeModal('personal-modal')" class="text-zinc-500 hover:text-white text-lg">✕</button>
    </div>

    <form onsubmit="savePersonalForm(event)" class="space-y-4">
      <input type="hidden" id="personal-form-id" />
      <div>
        <label class="block text-xs text-zinc-400 mb-1 mono">Провайдер / Сервис *</label>
        <input id="personal-form-provider" required placeholder="Например: GitHub, Сервер SSH, Лицензия"
          class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-white focus:border-emerald-400/60 focus:outline-none" />
      </div>
      <div>
        <label class="block text-xs text-zinc-400 mb-1 mono">Ключ / Значение *</label>
        <input id="personal-form-value" required placeholder="Введите сам ключ или пароль..."
          class="w-full px-3.5 py-2.5 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-emerald-300 mono focus:border-emerald-400/60 focus:outline-none" />
      </div>
      <div>
        <label class="block text-xs text-zinc-400 mb-1 mono">Примечание (логин, email, детали)</label>
        <input id="personal-form-notes" placeholder="Любая полезная информация..."
          class="w-full px-3.5 py-2 rounded-xl bg-zinc-900/80 border border-zinc-800 text-sm text-zinc-300 focus:border-emerald-400/60 focus:outline-none" />
      </div>

      <div class="flex justify-end gap-3 pt-3">
        <button type="button" onclick="closeModal('personal-modal')" class="px-4 py-2 rounded-xl bg-zinc-900 border border-zinc-700 text-xs text-zinc-300 mono">Отмена</button>
        <button type="submit" class="px-5 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold shadow-[0_0_15px_rgba(16,185,129,0.4)] text-xs uppercase mono">Сохранить</button>
      </div>
    </form>
  </div>
</div>

<div id="toast" class="fixed bottom-5 right-5 z-50 hidden px-4 py-3 rounded-xl bg-zinc-900/95 border border-amber-400/50 text-xs mono text-amber-300 shadow-[0_0_25px_rgba(245,158,11,0.25)]"></div>

<script>
let KEYS = [];
let APPS = [];
let PERSONAL = [];
let CURRENT_CAT = 'all';
let REVEALED = {};
let PING_STATUS = {};
let CLIP_INTERVAL = null;
let CURRENT_INTERCEPT = null;
let KEY_SEQ = 0;

async function api(path, body = null) {
  const opts = body ? { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) } : {};
  try {
    const res = await fetch(path, opts);
    const text = await res.text();
    try {
      return JSON.parse(text);
    } catch (e) {
      return { ok: false, msg: 'Сервер вернул не-JSON (HTTP ' + res.status + ')' };
    }
  } catch (e) {
    return { ok: false, msg: 'Нет связи с приложением' };
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  setTimeout(() => t.classList.add('hidden'), 3200);
}

async function openProviderSite(url) {
  if (!url) return;
  let target = url.trim();
  if (!target.startsWith('http://') && !target.startsWith('https://')) { target = 'https://' + target; }
  await api('/api/open_url', { url: target });
  showToast('🌐 Открыт сайт: ' + target);
}

async function initStatus() {
  const res = await api('/api/get_data');
  KEYS = res.keys || [];
  APPS = res.apps || [];
  PERSONAL = res.personal || [];
  
  const asToggle = document.getElementById('autostart-toggle');
  if (asToggle) asToggle.checked = !!res.autostart;

  if (KEYS.length > 0) {
    pingAllKeys();
  }
  
  setCategory('all');
}

setInterval(async () => {
  try {
    const res = await api('/api/poll_clipboard');
    if (res.detected) {
      CURRENT_INTERCEPT = res.detected;
      const masked = CURRENT_INTERCEPT.value.slice(0, 8) + '...' + CURRENT_INTERCEPT.value.slice(-4);
      document.getElementById('intercept-desc').textContent = `${CURRENT_INTERCEPT.detected_type} (${CURRENT_INTERCEPT.env_name} = ${masked})`;
      document.getElementById('intercept-banner').classList.remove('hidden');
    }
  } catch(e) {}
}, 2000);

function acceptInterceptedKey() {
  if (!CURRENT_INTERCEPT) return;
  document.getElementById('intercept-banner').classList.add('hidden');
  openModal({
    id: '', service: CURRENT_INTERCEPT.service, category: CURRENT_INTERCEPT.category,
    provider_url: CURRENT_INTERCEPT.provider_url, base_url: CURRENT_INTERCEPT.base_url,
    env_name: CURRENT_INTERCEPT.env_name, project: '', value: CURRENT_INTERCEPT.value,
    notes: 'Перехвачено из буфера'
  });
  CURRENT_INTERCEPT = null;
}

function dismissInterceptedKey() {
  CURRENT_INTERCEPT = null;
  document.getElementById('intercept-banner').classList.add('hidden');
}

async function grabClipboardNow() {
  const res = await api('/api/grab_clipboard', {});
  if (!res.ok) { showToast('⚠️ Буфер обмена пуст'); return; }
  openModal({
    id: '', service: res.candidate.service, category: res.candidate.category,
    provider_url: res.candidate.provider_url, base_url: res.candidate.base_url,
    env_name: res.candidate.env_name, project: '', value: res.candidate.value,
    notes: 'Импорт из буфера'
  });
  showToast('📥 Распознано: ' + res.candidate.detected_type);
}

async function toggleClipMonitor() {
  const enabled = document.getElementById('clip-monitor-toggle').checked;
  await api('/api/toggle_clip_monitor', { enabled });
  showToast(enabled ? '🛡️ Авто-перехват буфера включен' : '⏸️ Авто-перехват приостановлен');
}

async function toggleAutostart() {
  const enabled = document.getElementById('autostart-toggle').checked;
  const res = await api('/api/toggle_autostart', { enabled });
  if (res.ok) {
    showToast(enabled ? '🚀 Автозапуск с Windows Включен' : '⏸️ Автозапуск Отключен');
  } else {
    showToast('❌ Ошибка настройки автозапуска');
    document.getElementById('autostart-toggle').checked = !enabled;
  }
}

function toggleViewMode() {
  setCategory(CURRENT_CAT === 'list_view' ? 'all' : 'list_view');
}

function setCategory(cat) {
  CURRENT_CAT = cat;
  document.querySelectorAll('.cat-btn').forEach(btn => {
    btn.className = 'cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all text-zinc-400 hover:bg-zinc-900/60 hover:text-zinc-200 border border-transparent';
  });
  
  const activeBtn = document.getElementById('cat-' + cat);
  if (activeBtn) {
    if (cat === 'agents') {
      activeBtn.className = 'cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all bg-cyan-900/30 text-cyan-300 border border-cyan-500/40 shadow-[0_0_15px_rgba(8,145,178,0.15)] mt-1';
    } else if (cat === 'personal') {
      activeBtn.className = 'cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all bg-emerald-900/30 text-emerald-300 border border-emerald-500/40 shadow-[0_0_15px_rgba(16,185,129,0.15)] mt-1';
    } else {
      activeBtn.className = 'cat-btn w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all bg-amber-500/10 text-amber-400 border border-amber-500/30 shadow-[0_0_15px_rgba(245,158,11,0.1)]';
    }
  }

  const toggleBtn = document.getElementById('toggle-view-btn');
  toggleBtn.innerHTML = (cat === 'list_view') ? '<span>⊞</span> Вид карточками' : '<span>☰</span> Ключи списком';

  const addBtn = document.getElementById('main-add-btn');
  if (cat === 'agents') {
    addBtn.innerHTML = '<span>+</span> Новый Агент';
    addBtn.onclick = () => openAgentModal();
    addBtn.className = 'px-4 py-2 rounded-xl bg-cyan-600 hover:bg-cyan-500 text-white font-bold text-xs uppercase tracking-wider mono flex items-center gap-1.5 shadow-[0_0_15px_rgba(8,145,178,0.4)] transition-all';
  } else if (cat === 'personal') {
    addBtn.innerHTML = '<span>+</span> Личный ключ';
    addBtn.onclick = () => openPersonalModal();
    addBtn.className = 'px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs uppercase tracking-wider mono flex items-center gap-1.5 shadow-[0_0_15px_rgba(16,185,129,0.4)] transition-all';
  } else {
    addBtn.innerHTML = '<span>+</span> Новый ключ';
    addBtn.onclick = () => openModal();
    addBtn.className = 'px-4 py-2 rounded-xl amber-cta text-xs uppercase tracking-wider mono flex items-center gap-1.5';
  }

  document.getElementById('keys-view').classList.add('hidden');
  document.getElementById('list-view').classList.add('hidden');
  document.getElementById('env-builder-view').classList.add('hidden');
  document.getElementById('agents-view').classList.add('hidden');
  document.getElementById('personal-view').classList.add('hidden');

  if (cat === 'env_builder') {
    document.getElementById('env-builder-view').classList.remove('hidden');
    renderEnvBuilder();
  } else if (cat === 'list_view') {
    document.getElementById('list-view').classList.remove('hidden');
    renderListView();
  } else if (cat === 'agents') {
    document.getElementById('agents-view').classList.remove('hidden');
    renderAgents();
  } else if (cat === 'personal') {
    document.getElementById('personal-view').classList.remove('hidden');
    renderPersonal();
  } else {
    document.getElementById('keys-view').classList.remove('hidden');
    renderKeys();
  }
}

function renderCurrentView() {
  if (CURRENT_CAT === 'list_view') renderListView();
  else if (CURRENT_CAT === 'env_builder') renderEnvBuilder();
  else if (CURRENT_CAT === 'agents') renderAgents();
  else if (CURRENT_CAT === 'personal') renderPersonal();
  else renderKeys();
}

function maskValue(val) {
  if (!val) return '';
  if (val.length <= 10) return '••••••••••••';
  return val.slice(0, 6) + '••••••••••••' + val.slice(-4);
}

function toggleReveal(id) {
  REVEALED[id] = !REVEALED[id];
  renderCurrentView();
}

function updateCounters() {
  document.getElementById('count-all').textContent = KEYS.length;
  document.getElementById('count-ai').textContent = KEYS.filter(k => k.category === 'ai').length;
  document.getElementById('count-bots').textContent = KEYS.filter(k => k.category === 'bots').length;
  document.getElementById('count-cloud').textContent = KEYS.filter(k => k.category === 'cloud').length;
  if(document.getElementById('count-agents')) {
    document.getElementById('count-agents').textContent = APPS.length;
  }
  if(document.getElementById('count-personal')) {
    document.getElementById('count-personal').textContent = PERSONAL.length;
  }
}

function getFilteredKeys() {
  const q = (document.getElementById('search-input').value || '').toLowerCase();
  return KEYS.filter(k => {
    if (CURRENT_CAT !== 'all' && CURRENT_CAT !== 'list_view' && CURRENT_CAT !== 'agents' && CURRENT_CAT !== 'personal' && k.category !== CURRENT_CAT) return false;
    const text = `${k.service} ${k.env_name} ${k.project || ''} ${k.notes || ''} ${k.provider_url || ''} ${k.base_url || ''}`.toLowerCase();
    return text.includes(q);
  });
}

function renderProviderTitle(k) {
  const safeUrl = (k.provider_url || '').replace(/'/g, "\\'");
  if (safeUrl) {
    return `<button onclick="openProviderSite('${safeUrl}')" class="font-bold text-white hover:text-amber-400 text-base flex items-center gap-1.5 transition-colors text-left group" title="Перейти: ${safeUrl}"><span class="underline decoration-white/20 group-hover:decoration-amber-400/70 underline-offset-4">${k.service}</span><span class="text-xs text-amber-400/80 group-hover:translate-x-0.5 transition-transform">↗</span></button>`;
  }
  return `<h3 class="font-bold text-white text-base">${k.service}</h3>`;
}

function providerSignature(k) {
  return (k.service || '').trim().toLowerCase() + '|' + (k.category || '').trim().toLowerCase();
}

function groupKeysByProvider(list) {
  const map = new Map();
  list.forEach(k => {
    const sig = providerSignature(k);
    if (!map.has(sig)) map.set(sig, { service: k.service, category: k.category, keys: [] });
    map.get(sig).keys.push(k);
  });
  return Array.from(map.values());
}

function uniqueEnvName(k) {
  const base = (k.env_name || '').trim();
  if (!base) return base;
  const taken = new Set(KEYS.map(x => (x.env_name || '').trim().toLowerCase()));
  if (!taken.has(base.toLowerCase())) return base;
  let i = 2;
  while (taken.has(`${base}_${i}`.toLowerCase())) i += 1;
  return `${base}_${i}`;
}

function providerCountBadge(n) {
  if (n <= 1) return '';
  return `<span class="text-[10px] mono px-2 py-0.5 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-300 shrink-0" title="Ключей у этого провайдера: ${n}">&times;${n}</span>`;
}

function renderKeyBlock(k, group) {
  const isRev = !!REVEALED[k.id];
  const ping = PING_STATUS[k.id];
  let pingBadge = `<span class="w-2.5 h-2.5 rounded-full bg-zinc-600 shrink-0"></span>`;
  if (ping) {
    if (ping.checking) pingBadge = `<span class="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse shrink-0"></span>`;
    else if (ping.ok) pingBadge = `<span class="w-2.5 h-2.5 rounded-full bg-emerald-400 shadow-[0_0_8px_#34d399] shrink-0"></span>`;
    else pingBadge = `<span class="w-2.5 h-2.5 rounded-full bg-red-500 shadow-[0_0_8px_#ef4444] shrink-0"></span>`;
  }
  const baseUrlBtn = k.base_url
    ? `<button onclick="copyText('${k.base_url.replace(/'/g, "\\'")}', 'HTTP скопирован')" class="text-[11px] mono px-2 py-0.5 rounded bg-zinc-900/90 border border-zinc-700/70 text-zinc-300 hover:text-amber-300 truncate max-w-[260px]">🔗 ${k.base_url}</button>`
    : '';
  const groupKeys = (group && group.keys) || [k];
  const multi = groupKeys.length > 1;
  const hasExplicitPrimary = groupKeys.some(x => x.primary);
  const isPrimary = multi && (k.primary || (!hasExplicitPrimary && groupKeys[0].id === k.id));
  return `
    <div class="key-block flex flex-col shrink-0 rounded-xl border border-zinc-800/80 bg-black/25 p-3.5">
      <div class="flex items-start justify-between gap-2 mb-2">
        <div class="flex items-center gap-2 min-w-0 flex-wrap">
          ${pingBadge}
          <span class="text-xs mono text-amber-400/90">${k.env_name}</span>
          ${k.project ? `<span class="text-[10px] mono px-2 py-0.5 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-300">${k.project}</span>` : ''}
          ${multi && isPrimary ? `<span class="text-[10px] mono px-2 py-0.5 rounded-md bg-amber-500/15 border border-amber-400/40 text-amber-300">★ основной</span>` : ''}
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          ${multi ? `<button onclick="togglePrimary('${k.id}')" title="${isPrimary ? 'Снять статус основного' : 'Сделать основным ключом провайдера'}" class="px-2 py-1 rounded bg-zinc-900 hover:bg-zinc-800 text-xs ${isPrimary ? 'text-amber-400' : 'text-zinc-500 hover:text-amber-400'}">${isPrimary ? '★' : '☆'}</button>` : ''}
          ${multi ? `<button onclick="toggleRotation('${k.id}')" title="Ротация: склеивать ключи провайдера в одну переменную через запятую" class="px-2 py-1 rounded bg-zinc-900 hover:bg-zinc-800 text-xs ${k.rotation ? 'text-emerald-400' : 'text-zinc-500 hover:text-emerald-400'}">⇄</button>` : ''}
          <button onclick="editKey('${k.id}')" title="Редактировать ключ" class="px-2 py-1 rounded bg-zinc-900 hover:bg-zinc-800 text-zinc-400 hover:text-white text-xs">✎</button>
          <button onclick="deleteKey('${k.id}')" title="Удалить ключ" class="px-2 py-1 rounded bg-zinc-900 hover:bg-red-950 text-zinc-400 hover:text-red-400 text-xs">✕</button>
        </div>
      </div>
      <div class="flex items-center justify-between px-3.5 py-2.5 rounded-xl bg-black/60 border border-zinc-800/90 mono text-xs text-zinc-300">
        <span class="truncate pr-3 select-all">${isRev ? k.value : maskValue(k.value)}</span>
        <button onclick="toggleReveal('${k.id}')" class="text-zinc-500 hover:text-amber-400 shrink-0">${isRev ? '🙈' : '👁'}</button>
      </div>
      ${baseUrlBtn ? `<div class="mt-2">${baseUrlBtn}</div>` : ''}
      ${k.notes ? `<div onclick="editKey('${k.id}')" class="mt-2 text-[11px] text-zinc-400 truncate cursor-pointer hover:text-amber-400 transition-colors line-clamp-2" title="Нажмите, чтобы прочитать полностью или отредактировать">${k.notes}</div>` : ''}
      ${ping ? `<div class="mt-1.5 text-[11px] mono ${ping.checking ? 'text-amber-300' : (ping.ok ? 'text-emerald-400' : 'text-red-400')}">⚡ ${ping.msg}</div>` : ''}
      <div class="pt-3 flex flex-wrap items-center gap-2">
        <button onclick="copySecret('${k.id}', false)" class="flex-1 py-2 px-3 rounded-xl amber-cta text-xs mono text-center">📋 Ключ</button>
        <button onclick="copySecret('${k.id}', true)" class="py-2 px-3 rounded-xl bg-zinc-900 hover:bg-zinc-800 border border-zinc-700/70 text-xs text-zinc-200 mono transition-all">📄 .env</button>
        <button onclick="exportSetx('${k.id}')" class="py-2 px-3 rounded-xl bg-zinc-900 hover:bg-zinc-800 border border-zinc-700/70 text-xs text-amber-300 mono transition-all">🖥 SetX</button>
        <button onclick="pingKey('${k.id}')" class="py-2 px-2.5 rounded-xl bg-zinc-900 hover:bg-zinc-800 border border-zinc-700/70 text-xs text-zinc-300 mono transition-all">⚡</button>
      </div>
    </div>`;
}

function resolveProviderEnv(keys) {
  const out = [];
  groupKeysByProvider(keys).forEach(group => {
    const primary = group.keys.find(k => k.primary) || group.keys[0];
    const rotate = group.keys.some(k => k.rotation) && group.keys.length > 1;
    let value = primary.value || '';
    if (rotate) {
      const others = group.keys.filter(k => k !== primary).map(k => k.value).filter(Boolean);
      value = [value, ...others].filter(Boolean).join(',');
    }
    out.push({
      env_name: primary.env_name,
      value: value,
      base_url: primary.base_url || '',
      count: group.keys.length,
      rotate: rotate,
      primary_id: primary.id
    });
  });
  return out;
}

async function togglePrimary(id) {
  const k = KEYS.find(x => x.id === id);
  if (!k) return;
  const res = await api('/api/set_primary', { id: id, primary: !k.primary });
  if (!res.ok) { showToast('❌ ' + (res.msg || 'Ошибка')); return; }
  KEYS = res.keys || KEYS;
  renderCurrentView();
  showToast(res.primary ? '★ Ключ сделан основным' : '☆ Основной ключ снят');
}

async function toggleRotation(id) {
  const k = KEYS.find(x => x.id === id);
  if (!k) return;
  const res = await api('/api/set_rotation', { id: id, rotation: !k.rotation });
  if (!res.ok) { showToast('❌ ' + (res.msg || 'Ошибка')); return; }
  KEYS = res.keys || KEYS;
  renderCurrentView();
  showToast(res.rotation ? '⇄ Ротация включена: ключи склеиваются в одну переменную' : '⇄ Ротация выключена');
}

let IMPORT_ITEMS = [];

function openImportModal() {
  document.getElementById('import-modal').classList.remove('hidden');
  document.getElementById('import-results').innerHTML = '';
  IMPORT_ITEMS = [];
}

async function scanEnvFiles() {
  const folder = document.getElementById('import-folder').value.trim();
  if (!folder) { showToast('❌ Укажите папку проекта'); return; }
  const box = document.getElementById('import-results');
  box.innerHTML = `<div class="text-xs text-zinc-500 mono px-2 py-3">Сканирую...</div>`;
  const res = await api('/api/import_env_scan', { folder: folder });
  if (!res.ok) {
    box.innerHTML = `<div class="text-xs text-red-400 mono px-2 py-3">${res.msg || 'Ошибка сканирования'}</div>`;
    return;
  }
  IMPORT_ITEMS = res.items || [];
  if (!IMPORT_ITEMS.length) {
    box.innerHTML = `<div class="text-xs text-zinc-500 mono px-2 py-3">В .env ничего подходящего не найдено</div>`;
    return;
  }
  box.innerHTML = IMPORT_ITEMS.map((it, i) => `
    <label class="flex items-start gap-3 p-2.5 rounded-lg hover:bg-zinc-800/60 cursor-pointer border border-transparent hover:border-zinc-700">
      <input type="checkbox" class="import-chk accent-amber-500 w-4 h-4 mt-0.5" data-idx="${i}" ${/(KEY|TOKEN|SECRET|PASSWORD)/i.test(it.env_name) ? 'checked' : ''} />
      <div class="min-w-0 flex-1">
        <div class="text-xs mono text-amber-300">${it.env_name}</div>
        <div class="text-[10px] mono text-zinc-500 truncate">${it.file}:${it.line} · ${String(it.value).slice(0, 20)}••••</div>
      </div>
      ${it.hint && it.hint.service ? `<span class="text-[10px] mono px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-700/50 text-emerald-300 shrink-0">${it.hint.service}</span>` : ''}
    </label>`).join('');
}

async function importSelectedKeys() {
  const picked = [...document.querySelectorAll('.import-chk:checked')]
    .map(c => IMPORT_ITEMS[+c.dataset.idx])
    .filter(Boolean);
  if (!picked.length) { showToast('❌ Ничего не выбрано'); return; }
  const payload = picked.map(it => ({
    env_name: it.env_name,
    value: it.value,
    service: it.hint ? it.hint.service : '',
    category: it.hint ? it.hint.category : 'ai',
    provider_url: it.hint ? it.hint.provider_url : '',
    base_url: it.hint ? it.hint.base_url : '',
    file: it.file
  }));
  const res = await api('/api/import_env_save', { items: payload });
  if (!res.ok) { showToast('❌ ' + (res.msg || 'Ошибка импорта')); return; }
  KEYS = res.keys || KEYS;
  closeModal('import-modal');
  renderCurrentView();
  showToast(`✅ Импортировано ключей: ${res.added}`);
}

function renderKeys() {
  updateCounters();
  const container = document.getElementById('keys-view');
  container.innerHTML = '';
  const filtered = getFilteredKeys();
  if (filtered.length === 0) {
    container.innerHTML = `<div class="col-span-full text-center py-16 text-zinc-500 mono text-sm">Ключи не найдены. Нажмите «+ Новый ключ».</div>`;
    return;
  }
  groupKeysByProvider(filtered).forEach(group => {
    const first = group.keys[0];
    const card = document.createElement('div');
    card.className = 'hud-card h-full rounded-2xl p-5 flex flex-col';
    card.innerHTML = `
      <span class="hud-corner-tl"></span><span class="hud-corner-tr"></span><span class="hud-corner-bl"></span><span class="hud-corner-br"></span>
      <div class="flex flex-col flex-1 gap-3 min-h-0">
        <div class="flex items-start justify-between gap-2">
          <div class="flex items-center gap-2.5">${renderProviderTitle(first)}${providerCountBadge(group.keys.length)}</div>
          <div class="flex items-center gap-1.5 shrink-0">
            <button onclick="addExtraKey('${first.id}')" title="Добавить ещё ключ для этого провайдера" class="px-2 py-1 rounded bg-zinc-900 hover:bg-amber-950 text-zinc-400 hover:text-amber-400 text-xs">+</button>
          </div>
        </div>
        <div class="flex-1 flex flex-col gap-3 min-h-0 max-h-[22rem] overflow-y-auto pr-1">${group.keys.map(k => renderKeyBlock(k, group)).join('')}</div>
      </div>
    `;
    container.appendChild(card);
  });
}

function renderListView() {
  updateCounters();
  const tbody = document.getElementById('list-table-body');
  tbody.innerHTML = '';
  const filtered = getFilteredKeys();
  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="py-10 text-center text-zinc-500 mono text-xs">Ключи не найдены</td></tr>`;
    return;
  }
  filtered.forEach(k => {
    const isRev = !!REVEALED[k.id];
    const ping = PING_STATUS[k.id];
    const safeUrl = (k.provider_url || '').replace(/'/g, "\\'");
    let statusHtml = `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-zinc-800/80 border border-zinc-700 text-zinc-400 text-xs mono"><span class="w-2 h-2 rounded-full bg-zinc-500"></span> Не проверен</span>`;
    if (ping) {
      if (ping.checking) statusHtml = `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-amber-500/10 border border-amber-400/30 text-amber-300 text-xs mono"><span class="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span> Проверка...</span>`;
      else if (ping.ok) statusHtml = `<div class="space-y-0.5"><span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-md bg-emerald-950/60 border border-emerald-700/60 text-emerald-300 text-xs mono"><span class="w-2 h-2 rounded-full bg-emerald-400"></span> РАБОЧИЙ</span><div class="text-[11px] text-zinc-400 mono">${ping.msg}</div></div>`;
      else statusHtml = `<div class="space-y-0.5"><span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-md bg-red-950/60 border border-red-800/60 text-red-300 text-xs mono"><span class="w-2 h-2 rounded-full bg-red-500"></span> НЕРАБОЧИЙ</span><div class="text-[11px] text-red-400/80 mono">${ping.msg}</div></div>`;
    }
    const providerCell = safeUrl
      ? `<button onclick="openProviderSite('${safeUrl}')" class="font-semibold text-white hover:text-amber-400 flex items-center gap-1.5 group text-left"><span class="underline decoration-white/20 group-hover:decoration-amber-400">${k.service}</span><span class="text-xs text-amber-400">↗</span></button>`
      : `<div class="font-semibold text-white">${k.service}</div>`;
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-white/[0.02] transition-colors';
    tr.innerHTML = `
      <td class="py-3.5 px-4 align-top">${providerCell}${k.project ? `<span class="inline-block mt-1 text-[10px] mono px-2 py-0.5 rounded bg-zinc-900 border border-zinc-700 text-zinc-400">${k.project}</span>` : ''}</td>
      <td class="py-3.5 px-4 align-top mono"><div class="text-xs text-amber-400">${k.env_name}</div>${k.base_url ? `<div class="text-[11px] text-zinc-400 mt-0.5 truncate max-w-[220px]">🔗 ${k.base_url}</div>` : `<div class="text-[11px] text-zinc-600 mt-0.5">Стандартный HTTP</div>`}</td>
      <td class="py-3.5 px-4 align-top mono text-xs text-zinc-300"><div class="flex items-center gap-2"><span class="select-all">${isRev ? k.value : maskValue(k.value)}</span><button onclick="toggleReveal('${k.id}')" class="text-zinc-500 hover:text-amber-300">${isRev ? '🙈' : '👁'}</button></div></td>
      <td class="py-3.5 px-4 align-top">${statusHtml}</td>
      <td class="py-3.5 px-4 align-top text-right whitespace-nowrap space-x-1">
        <button onclick="copySecret('${k.id}', false)" class="px-2.5 py-1.5 rounded-lg amber-cta text-xs mono">📋</button>
        <button onclick="pingKey('${k.id}')" class="px-2.5 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-xs text-zinc-200 mono">⚡</button>
        <button onclick="addExtraKey('${k.id}')" title="Добавить ещё ключ для этого провайдера" class="px-2 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-xs text-zinc-400 hover:text-amber-400">+</button>
        <button onclick="editKey('${k.id}')" class="px-2 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-xs text-zinc-400 hover:text-white">✎</button>
        <button onclick="deleteKey('${k.id}')" class="px-2 py-1.5 rounded-lg bg-zinc-900 hover:bg-red-950 border border-zinc-800 text-xs text-zinc-400 hover:text-red-400">✕</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

const AGENT_ICON_CACHE = {};

function agentIconHtml(app) {
  const fallback = `<span class="text-2xl leading-none">${app.icon || '🤖'}</span>`;
  const key = app.path || '';
  const cached = AGENT_ICON_CACHE[key];
  if (cached && cached.state === 'ready') {
    return `<img src="${cached.data}" class="w-8 h-8 object-contain" alt="${app.name || ''}">`;
  }
  if (!cached) loadAgentIcon(app);
  return fallback;
}

async function loadAgentIcon(app) {
  const key = app.path || '';
  if (!key) return;
  AGENT_ICON_CACHE[key] = { state: 'loading' };
  try {
    const res = await api('/api/agent_icon', { path: key });
    if (res && res.ok && res.icon) {
      AGENT_ICON_CACHE[key] = { state: 'ready', data: res.icon };
      renderCurrentView();
      const menu = document.getElementById('quick-launch-menu');
      if (menu && !menu.classList.contains('hidden')) renderQuickLaunchMenu();
    } else {
      AGENT_ICON_CACHE[key] = { state: 'none' };
    }
  } catch (e) {
    AGENT_ICON_CACHE[key] = { state: 'none' };
  }
}

function renderQuickLaunchMenu() {
  const menu = document.getElementById('quick-launch-menu');
  if (!menu) return;
  menu.innerHTML = '';
  if (!APPS.length) {
    menu.innerHTML = `<div class="text-[11px] text-zinc-500 mono px-2 py-3 text-center">Агенты не заданы.<br>Добавьте их во вкладке «Агенты ИИ».</div>`;
    return;
  }
  APPS.forEach(a => {
    const item = document.createElement('button');
    item.className = 'w-full flex items-center gap-2 px-2 py-2 rounded-lg hover:bg-cyan-900/30 text-left transition-colors';
    item.innerHTML = `${agentIconHtml(a)}<span class="text-xs text-white truncate">${a.name || ''}</span>`;
    item.onclick = async () => {
      menu.classList.add('hidden');
      await launchApp(a.path, a.id);
    };
    menu.appendChild(item);
  });
}

function toggleQuickLaunch() {
  const menu = document.getElementById('quick-launch-menu');
  if (!menu) return;
  if (!menu.classList.contains('hidden')) {
    menu.classList.add('hidden');
    return;
  }
  renderQuickLaunchMenu();
  menu.classList.remove('hidden');
}

document.addEventListener('click', (e) => {
  const menu = document.getElementById('quick-launch-menu');
  if (!menu || menu.classList.contains('hidden')) return;
  if (menu.contains(e.target)) return;
  if (e.target.closest && e.target.closest('#quick-launch-btn')) return;
  menu.classList.add('hidden');
});

function renderAgents() {
  updateCounters();
  const container = document.getElementById('agents-view');
  container.innerHTML = '';
  
  const q = (document.getElementById('search-input').value || '').toLowerCase();
  const filtered = APPS.filter(a => {
    return a.name.toLowerCase().includes(q) || a.path.toLowerCase().includes(q);
  });

  if (filtered.length === 0) {
    container.innerHTML = `<div class="col-span-full text-center py-16 text-zinc-500 mono text-sm">Агенты ИИ не найдены. Добавьте вручную.</div>`;
    return;
  }

  filtered.forEach(app => {
    let badges = '';
    let conflictCount = 0;
    
    app.linked_keys.forEach(kid => {
      const k = KEYS.find(x => x.id === kid);
      if (!k) {
        badges += `<div class="text-[10px] bg-red-950/60 text-red-400 border border-red-800/80 px-2 py-1 rounded truncate w-full flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-red-500"></span> <s>Удаленный ключ</s></div>`;
        conflictCount++;
      } else {
        const ping = PING_STATUS[kid];
        if (ping && !ping.ok && !ping.checking) {
          badges += `<div class="text-[10px] bg-red-950/60 text-red-400 border border-red-800/80 px-2 py-1 rounded truncate w-full flex items-center gap-1.5" title="${ping.msg}"><span class="w-1.5 h-1.5 rounded-full bg-red-500"></span> ${k.service} (${k.env_name})</div>`;
          conflictCount++;
        } else if (ping && ping.checking) {
          badges += `<div class="text-[10px] bg-amber-950/60 text-amber-400 border border-amber-800/80 px-2 py-1 rounded truncate w-full flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"></span> ${k.service}</div>`;
        } else {
          badges += `<div class="text-[10px] bg-emerald-950/60 text-emerald-400 border border-emerald-800/80 px-2 py-1 rounded truncate w-full flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span> ${k.service}</div>`;
        }
      }
    });

    if (app.linked_keys.length === 0) {
      badges = `<div class="text-[10px] text-zinc-600 mono italic">Нет привязанных ключей</div>`;
    }

    let statusIndicator = conflictCount > 0 
      ? `<span class="w-3 h-3 rounded-full bg-red-500 shadow-[0_0_8px_#ef4444] shrink-0" title="Есть конфликты в ключах!"></span>`
      : `<span class="w-3 h-3 rounded-full bg-emerald-400 shadow-[0_0_8px_#34d399] shrink-0" title="Все ок"></span>`;

    const card = document.createElement('div');
    card.className = 'hud-card h-full rounded-2xl p-5 flex flex-col border-t-2 border-t-cyan-500/40';
    card.innerHTML = `
      <div class="flex flex-1 flex-col min-h-0">
        <div class="flex items-start justify-between gap-3 mb-3">
          <div class="flex items-center gap-3">
            <button onclick="launchApp('${app.path.replace(/'/g, "\\'")}', '${app.id}')" class="w-12 h-12 rounded-xl bg-cyan-950/50 hover:bg-cyan-900 border border-cyan-800/50 hover:border-cyan-400 flex items-center justify-center shadow-[0_0_15px_rgba(8,145,178,0.2)] hover:shadow-[0_0_20px_rgba(8,145,178,0.6)] shrink-0 transition-all cursor-pointer" title="Кликните чтобы запустить">
              ${agentIconHtml(app)}
            </button>
            <div class="min-w-0">
              <h3 class="font-bold text-white text-base leading-tight truncate">${app.name}</h3>
              <div class="text-[10px] text-cyan-400/80 mono mt-0.5 truncate max-w-[150px]" title="${app.path}">${app.path}</div>
            </div>
          </div>
          ${statusIndicator}
        </div>
        
        <div class="text-[10px] uppercase tracking-widest text-zinc-500 mb-1.5 mt-4 mono">API-Зависимости:</div>
        <div class="flex flex-col gap-1.5 max-h-24 overflow-y-auto pr-1">
          ${badges}
        </div>
      </div>

      <div class="pt-4 border-t border-white/5 flex items-center justify-between gap-2 mt-auto">
        <div class="flex gap-1.5">
          <button onclick="editAgent('${app.id}')" class="px-2.5 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-400 hover:text-white text-xs">✎</button>
          <button onclick="deleteAgent('${app.id}')" class="px-2.5 py-1.5 rounded-lg bg-zinc-900 hover:bg-red-950 text-zinc-400 hover:text-red-400 text-xs">✕</button>
        </div>
        <button onclick="launchApp('${app.path.replace(/'/g, "\\'")}', '${app.id}')" class="flex-1 py-2 px-3 rounded-xl bg-cyan-600 hover:bg-cyan-500 text-white font-bold shadow-[0_0_15px_rgba(8,145,178,0.4)] text-xs mono text-center uppercase tracking-wider transition-all">
          ▶ Запустить
        </button>
      </div>
    `;
    container.appendChild(card);
  });
}

function renderPersonal() {
  updateCounters();
  const container = document.getElementById('personal-view');
  container.innerHTML = '';
  
  const q = (document.getElementById('search-input').value || '').toLowerCase();
  const filtered = PERSONAL.filter(k => {
    const text = `${k.provider} ${k.notes || ''}`.toLowerCase();
    return text.includes(q);
  });

  if (filtered.length === 0) {
    container.innerHTML = `<div class="col-span-full text-center py-16 text-zinc-500 mono text-sm">Личные ключи не найдены. Добавьте вручную.</div>`;
    return;
  }

  filtered.forEach(k => {
    const isRev = !!REVEALED[k.id];
    const card = document.createElement('div');
    card.className = 'hud-card h-full rounded-2xl p-5 flex flex-col justify-between space-y-4 border-t-2 border-t-emerald-500/40';
    card.innerHTML = `
      <div>
        <div class="flex items-start justify-between gap-2 mb-3">
          <h3 class="font-bold text-white text-base leading-tight flex items-center gap-2">
            <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 shadow-[0_0_8px_#10b981]"></span>
            ${k.provider}
          </h3>
          <div class="flex items-center gap-1.5 shrink-0">
            <button onclick="editPersonal('${k.id}')" class="px-2 py-1 rounded bg-zinc-900 hover:bg-zinc-800 text-zinc-400 hover:text-white text-xs">✎</button>
            <button onclick="deletePersonal('${k.id}')" class="px-2 py-1 rounded bg-zinc-900 hover:bg-red-950 text-zinc-400 hover:text-red-400 text-xs">✕</button>
          </div>
        </div>
        
        <div class="flex items-center justify-between px-3.5 py-2.5 rounded-xl bg-black/60 border border-zinc-800/90 mono text-xs text-emerald-300">
          <span class="truncate pr-3 select-all">${isRev ? k.value : maskValue(k.value)}</span>
          <button onclick="toggleReveal('${k.id}')" class="text-zinc-500 hover:text-emerald-400 shrink-0">${isRev ? '🙈' : '👁'}</button>
        </div>
        
        ${k.notes ? `<div class="mt-3 flex items-start gap-2">
          <div onclick="openPersonalNote('${k.id}')" class="flex-1 text-[11px] text-zinc-400 p-2.5 rounded-lg bg-zinc-900/50 border border-zinc-800/50 cursor-pointer hover:border-emerald-500/40 hover:text-zinc-300 transition-colors line-clamp-2" title="Нажмите, чтобы открыть в блокноте">📝 ${k.notes}</div>
          <button onclick="syncPersonalNote('${k.id}')" title="Забрать изменения из блокнота" class="px-2 py-1.5 rounded bg-zinc-900 hover:bg-zinc-800 text-zinc-400 hover:text-emerald-400 text-xs shrink-0">⟳</button>
        </div>` : ''}
      </div>
      
      <div class="pt-3 border-t border-white/5 mt-auto flex">
        <button onclick="copyPersonalSecret('${k.id}')" class="flex-1 py-2 px-3 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold shadow-[0_0_15px_rgba(16,185,129,0.3)] text-xs mono text-center uppercase tracking-wider transition-all">
          📋 Скопировать
        </button>
      </div>
    `;
    container.appendChild(card);
  });
}

function startClipboardTimer(seconds = 15) {
  if (CLIP_INTERVAL) clearInterval(CLIP_INTERVAL);
  const box = document.getElementById('clipboard-timer');
  const secSpan = document.getElementById('timer-sec');
  box.classList.remove('hidden'); box.classList.add('flex');
  let rem = seconds; secSpan.textContent = rem;
  CLIP_INTERVAL = setInterval(async () => {
    rem--; secSpan.textContent = rem;
    if (rem <= 0) {
      clearInterval(CLIP_INTERVAL); box.classList.add('hidden');
      try { await navigator.clipboard.writeText(''); } catch(e) {}
      showToast('🧹 Буфер очищен');
    }
  }, 1000);
}

async function copyText(text, msg) {
  await api('/api/ignore_token', { token: text });
  await navigator.clipboard.writeText(text);
  showToast(msg);
}

async function copySecret(id, asEnvLine) {
  const k = KEYS.find(x => x.id === id);
  if (!k) return;
  const text = asEnvLine ? `${k.env_name}="${k.value}"` : k.value;
  await api('/api/ignore_token', { token: k.value });
  await navigator.clipboard.writeText(text);
  showToast(asEnvLine ? `Скопировано: ${k.env_name}="..."` : `Ключ скопирован!`);
  startClipboardTimer(15);
}

async function copyPersonalSecret(id) {
  const k = PERSONAL.find(x => x.id === id);
  if (!k) return;
  await api('/api/ignore_token', { token: k.value });
  await navigator.clipboard.writeText(k.value);
  showToast(`📋 Личный ключ скопирован!`);
  startClipboardTimer(15);
}

async function openPersonalNote(id) {
  const res = await api('/api/open_note', { id });
  if (res.ok) showToast('📝 Заметка открыта в блокноте');
  else showToast(`❌ ${res.msg || 'Не удалось открыть заметку'}`);
}

async function syncPersonalNote(id) {
  const res = await api('/api/sync_note', { id });
  if (res.ok) {
    PERSONAL = res.personal || PERSONAL;
    renderCurrentView();
    showToast('🔄 Заметка обновлена из блокнота');
  } else {
    showToast(`❌ ${res.msg || 'Не удалось забрать заметку'}`);
  }
}

async function exportSetx(id) {
  const k = KEYS.find(x => x.id === id);
  if (!k) return;
  const res = await api('/api/setx', { env_name: k.env_name, value: k.value });
  showToast(res.msg);
}

async function pingKey(id) {
  PING_STATUS[id] = { ok: false, checking: true, msg: 'Проверка...' };
  renderCurrentView();
  PING_STATUS[id] = await api('/api/ping', { id });
  renderCurrentView();
}

async function pingAllKeys() { 
  for (const k of KEYS) { 
    pingKey(k.id); 
  } 
}

async function launchApp(path, appId) {
  const res = await api('/api/launch_app', { path: path, app_id: appId || null });
  if (res.ok) showToast('🚀 ' + (res.msg || 'Запущено'));
  else showToast('❌ ' + (res.msg || 'Ошибка запуска'));
}

function openModal(editObj = null, mode = '') {
  document.getElementById('key-modal').classList.remove('hidden');
  const modalTitle = document.getElementById('modal-title');
  if (mode === 'extra') {
    modalTitle.innerHTML = `+ Дополнительный ключ &middot; <span class="text-amber-400">${(editObj && editObj.service) || ''}</span>`;
  } else {
    modalTitle.innerHTML = editObj && editObj.id ? '✎ Редактировать <span class="text-amber-400">Ключ</span>' : '+ Добавить <span class="text-amber-400">API Ключ</span>';
  }
  document.getElementById('form-id').value = editObj ? editObj.id : '';
  document.getElementById('form-service').value = editObj ? (editObj.service || '') : '';
  document.getElementById('form-category').value = editObj ? (editObj.category || 'ai') : 'ai';
  document.getElementById('form-provider-url').value = editObj ? (editObj.provider_url || '') : '';
  document.getElementById('form-base-url').value = editObj ? (editObj.base_url || '') : '';
  document.getElementById('form-env').value = editObj ? (editObj.env_name || '') : '';
  document.getElementById('form-project').value = editObj ? (editObj.project || '') : '';
  document.getElementById('form-value').value = editObj ? (editObj.value || '') : '';
  document.getElementById('form-notes').value = editObj ? (editObj.notes || '') : '';
}

function openAgentModal(editObj = null) {
  document.getElementById('agent-modal').classList.remove('hidden');
  document.getElementById('agent-modal-title').innerHTML = editObj && editObj.id ? '✎ Редактировать <span class="text-cyan-400">Агента</span>' : '+ Добавить <span class="text-cyan-400">Агента</span>';
  document.getElementById('agent-form-id').value = editObj ? editObj.id : '';
  document.getElementById('agent-form-name').value = editObj ? (editObj.name || '') : '';
  document.getElementById('agent-form-icon').value = editObj ? (editObj.icon || '🤖') : '🤖';
  document.getElementById('agent-form-path').value = editObj ? (editObj.path || '') : '';
  
  const container = document.getElementById('agent-keys-checkboxes');
  container.innerHTML = '';
  const linked = editObj && editObj.linked_keys ? editObj.linked_keys : [];
  
  KEYS.forEach(k => {
    const isChecked = linked.includes(k.id) ? 'checked' : '';
    const div = document.createElement('label');
    div.className = 'flex items-center gap-3 p-2 hover:bg-zinc-800 rounded cursor-pointer';
    div.innerHTML = `
      <input type="checkbox" value="${k.id}" class="agent-key-chk accent-cyan-500 w-4 h-4" ${isChecked} />
      <div class="flex-1 overflow-hidden">
        <div class="text-[11px] text-white font-bold truncate">${k.service}</div>
        <div class="text-[10px] text-amber-400 mono truncate">${k.env_name}</div>
      </div>
    `;
    container.appendChild(div);
  });
}

function openPersonalModal(editObj = null) {
  document.getElementById('personal-modal').classList.remove('hidden');
  document.getElementById('personal-modal-title').innerHTML = editObj && editObj.id ? '✎ Редактировать <span class="text-emerald-400">Личный ключ</span>' : '+ Добавить <span class="text-emerald-400">Личный ключ</span>';
  document.getElementById('personal-form-id').value = editObj ? editObj.id : '';
  document.getElementById('personal-form-provider').value = editObj ? (editObj.provider || '') : '';
  document.getElementById('personal-form-value').value = editObj ? (editObj.value || '') : '';
  document.getElementById('personal-form-notes').value = editObj ? (editObj.notes || '') : '';
}

function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

function editKey(id) { const k = KEYS.find(x => x.id === id); if (k) openModal(k); }
function addExtraKey(id) {
  const k = KEYS.find(x => x.id === id);
  if (!k) return;
  openModal({
    id: '',
    service: k.service || '',
    category: k.category || 'ai',
    provider_url: k.provider_url || '',
    base_url: k.base_url || '',
    env_name: uniqueEnvName(k),
    project: k.project || '',
    value: '',
    notes: ''
  }, 'extra');
}
function editAgent(id) { const a = APPS.find(x => x.id === id); if (a) openAgentModal(a); }
function editPersonal(id) { const k = PERSONAL.find(x => x.id === id); if (k) openPersonalModal(k); }

function newKeyId() { KEY_SEQ += 1; return 'k_' + Date.now() + '_' + KEY_SEQ; }

async function saveKeyForm(e) {
  e.preventDefault();
  const payload = {
    id: document.getElementById('form-id').value || newKeyId(),
    service: document.getElementById('form-service').value.trim(),
    category: document.getElementById('form-category').value,
    provider_url: document.getElementById('form-provider-url').value.trim(),
    base_url: document.getElementById('form-base-url').value.trim(),
    env_name: document.getElementById('form-env').value.trim(),
    project: document.getElementById('form-project').value.trim(),
    value: document.getElementById('form-value').value.trim(),
    notes: document.getElementById('form-notes').value.trim(),
  };
  const res = await api('/api/save_key', payload);
  if (res.ok) { KEYS = res.keys; closeModal('key-modal'); renderCurrentView(); showToast('✅ Ключ сохранен в vault.json'); }
}

async function saveAgentForm(e) {
  e.preventDefault();
  const checkedKeys = Array.from(document.querySelectorAll('.agent-key-chk:checked')).map(cb => cb.value);
  const payload = {
    id: document.getElementById('agent-form-id').value || ('app_' + Date.now()),
    name: document.getElementById('agent-form-name').value.trim(),
    icon: document.getElementById('agent-form-icon').value.trim() || '🤖',
    path: document.getElementById('agent-form-path').value.trim(),
    linked_keys: checkedKeys
  };
  const res = await api('/api/save_app', payload);
  if (res.ok) { APPS = res.apps; closeModal('agent-modal'); renderCurrentView(); showToast('✅ Агент сохранен'); }
}

async function savePersonalForm(e) {
  e.preventDefault();
  const payload = {
    id: document.getElementById('personal-form-id').value || ('pk_' + Date.now()),
    provider: document.getElementById('personal-form-provider').value.trim(),
    value: document.getElementById('personal-form-value').value.trim(),
    notes: document.getElementById('personal-form-notes').value.trim()
  };
  const res = await api('/api/save_personal', payload);
  if (res.ok) { PERSONAL = res.personal; closeModal('personal-modal'); renderCurrentView(); showToast('✅ Личный ключ сохранен'); }
}

async function deleteKey(id) {
  if (!confirm('Удалить этот API-ключ?')) return;
  const res = await api('/api/delete_key', { id });
  if (res.ok) { KEYS = res.keys; APPS = res.apps; renderCurrentView(); showToast('🗑 Ключ удален'); }
}

async function deleteAgent(id) {
  if (!confirm('Удалить этого агента?')) return;
  const res = await api('/api/delete_app', { id });
  if (res.ok) { APPS = res.apps; renderCurrentView(); showToast('🗑 Агент удален'); }
}

async function deletePersonal(id) {
  if (!confirm('Удалить этот личный ключ?')) return;
  const res = await api('/api/delete_personal', { id });
  if (res.ok) { PERSONAL = res.personal; renderCurrentView(); showToast('🗑 Ключ удален'); }
}

function renderEnvBuilder() {
  const list = document.getElementById('env-checkbox-list');
  list.innerHTML = '';
  const groups = groupKeysByProvider(KEYS);
  if (!groups.length) {
    list.innerHTML = `<div class="text-xs text-zinc-500 mono px-2 py-3">Ключей нет</div>`;
    return;
  }
  groups.forEach(group => {
    const canonical = group.keys[0];
    const primary = group.keys.find(k => k.primary) || canonical;
    const rotate = group.keys.some(k => k.rotation) && group.keys.length > 1;
    const row = document.createElement('label');
    row.className = 'flex items-center justify-between gap-3 p-3 rounded-xl bg-zinc-900/60 border border-zinc-800 hover:border-amber-500/40 cursor-pointer';
    row.innerHTML = `
      <div class="flex items-center gap-3 min-w-0">
        <input type="checkbox" checked value="${providerSignature(canonical)}" onchange="updateEnvPreview()" class="env-chk accent-[#355C69] w-4 h-4" />
        <div class="min-w-0">
          <div class="text-sm font-medium text-white truncate">${canonical.service}</div>
          <div class="text-xs mono text-amber-400 truncate">${canonical.env_name}</div>
        </div>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        ${group.keys.length > 1 ? `<span class="text-[10px] mono px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300" title="Ключей у провайдера">×${group.keys.length}</span>` : ''}
        ${rotate ? `<span class="text-[10px] mono px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-700/50 text-emerald-300" title="Ротация: все ключи одной переменной через запятую">⇄</span>` : ''}
        <span class="text-xs mono text-zinc-400">${canonical.project || ''}</span>
      </div>`;
    list.appendChild(row);
  });
  updateEnvPreview();
}

function updateEnvPreview() {
  const checked = [...document.querySelectorAll('.env-chk:checked')].map(c => c.value);
  const lines = ['# Generated by CYBER-VAULT'];
  groupKeysByProvider(KEYS).forEach(group => {
    const canonical = group.keys[0];
    if (!checked.includes(providerSignature(canonical))) return;
    const primary = group.keys.find(k => k.primary) || canonical;
    const rotate = group.keys.some(k => k.rotation) && group.keys.length > 1;
    let value = primary.value || '';
    if (rotate) {
      const others = group.keys.filter(k => k !== primary).map(k => k.value).filter(Boolean);
      value = [value, ...others].filter(Boolean).join(',');
    }
    lines.push(`${canonical.env_name}="${value}"`);
    if (canonical.base_url) {
      lines.push(`${canonical.env_name.replace(/_API_KEY$|_KEY$|_TOKEN$/, '') + '_BASE_URL'}="${canonical.base_url}"`);
    }
  });
  document.getElementById('env-preview').value = lines.join('\n');
}

async function copyGeneratedEnv() {
  await navigator.clipboard.writeText(document.getElementById('env-preview').value);
  showToast('📋 Конфиг .env скопирован в буфер!');
}

async function saveEnvToFile() {
  const res = await api('/api/export_env_file', { folder: document.getElementById('export-dir-path').value.trim(), content: document.getElementById('env-preview').value });
  showToast(res.msg);
}

initStatus();
</script>
</body>
</html>

"""


class VaultServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class VaultRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            raw = HTML_UI.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/get_data":
            self._send_json({
                "ok": True, 
                "keys": VAULT.data.get("keys", []),
                "apps": VAULT.data.get("apps", []),
                "personal": VAULT.data.get("personal", []),
                "autostart": is_autostart_enabled()
            })
        elif self.path == "/api/poll_clipboard":
            detected = DETECTED_KEY_QUEUE.pop(0) if DETECTED_KEY_QUEUE else None
            self._send_json({"detected": detected})
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        try:
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            body = json.loads(raw_body) if raw_body.strip() else {}
            if not isinstance(body, dict):
                body = {}
        except Exception:
            self._send_json({"ok": False, "msg": "Некорректный JSON в теле запроса"}, status=400)
            return
        try:
            self._handle_post(body)
        except Exception as e:
            try:
                self._send_json({"ok": False, "msg": "Внутренняя ошибка: %s" % str(e)[:120]}, status=500)
            except Exception:
                pass

    def _handle_post(self, body):
        global CLIPBOARD_MONITOR_ENABLED

        if self.path == "/api/save_key":
            if not body.get("env_name") or not body.get("value"):
                self._send_json({"ok": False, "msg": "Укажите имя переменной и значение ключа"})
                return
            keys = VAULT.data.get("keys", [])
            key_id = body.get("id") or ("k_%d" % int(time.time() * 1000))
            item = dict(body)
            item["id"] = key_id
            existing_idx = next((i for i, k in enumerate(keys) if k.get("id") == key_id), None)
            if existing_idx is not None:
                keys[existing_idx] = item
            else:
                keys.append(item)
            VAULT.data["keys"] = keys
            VAULT.save()
            IGNORED_TOKENS.add(body.get("value", ""))
            self._send_json({"ok": True, "keys": keys})

        elif self.path == "/api/save_app":
            apps = VAULT.data.get("apps", [])
            app_id = body.get("id") or ("app_%d" % int(time.time() * 1000))
            item = dict(body)
            item["id"] = app_id
            item.setdefault("linked_keys", [])
            existing_idx = next((i for i, a in enumerate(apps) if a.get("id") == app_id), None)
            if existing_idx is not None:
                apps[existing_idx] = item
            else:
                apps.append(item)
            VAULT.data["apps"] = apps
            VAULT.save()
            self._send_json({"ok": True, "apps": apps})

        elif self.path == "/api/save_personal":
            if not body.get("value"):
                self._send_json({"ok": False, "msg": "Укажите значение личного ключа"})
                return
            items = VAULT.data.get("personal", [])
            item_id = body.get("id") or ("pk_%d" % int(time.time() * 1000))
            item = dict(body)
            item["id"] = item_id
            existing_idx = next((i for i, k in enumerate(items) if k.get("id") == item_id), None)
            if existing_idx is not None:
                items[existing_idx] = item
            else:
                items.append(item)
            VAULT.data["personal"] = items
            VAULT.save()
            IGNORED_TOKENS.add(body.get("value", ""))
            self._send_json({"ok": True, "personal": items})

        elif self.path == "/api/delete_key":
            keys = [k for k in VAULT.data.get("keys", []) if k.get("id") != body.get("id")]
            apps = VAULT.data.get("apps", [])
            # Also remove this key from all linked apps
            for a in apps:
                if body.get("id") in a.get("linked_keys", []):
                    a["linked_keys"].remove(body.get("id"))
            VAULT.data["keys"] = keys
            VAULT.data["apps"] = apps
            VAULT.save()
            self._send_json({"ok": True, "keys": keys, "apps": apps})

        elif self.path == "/api/delete_app":
            apps = [a for a in VAULT.data.get("apps", []) if a.get("id") != body.get("id")]
            VAULT.data["apps"] = apps
            VAULT.save()
            self._send_json({"ok": True, "apps": apps})

        elif self.path == "/api/delete_personal":
            items = [k for k in VAULT.data.get("personal", []) if k.get("id") != body.get("id")]
            VAULT.data["personal"] = items
            VAULT.save()
            self._send_json({"ok": True, "personal": items})

        elif self.path == "/api/ignore_token":
            tok = body.get("token", "")
            if tok:
                IGNORED_TOKENS.add(tok)
            self._send_json({"ok": True})

        elif self.path == "/api/toggle_clip_monitor":
            CLIPBOARD_MONITOR_ENABLED = bool(body.get("enabled", True))
            self._send_json({"ok": True, "enabled": CLIPBOARD_MONITOR_ENABLED})

        elif self.path == "/api/grab_clipboard":
            clip = pyperclip.paste().strip() if pyperclip else ""
            if not clip:
                self._send_json({"ok": False})
                return
            cand = parse_clipboard_candidate(clip)
            if not cand:
                cand = {
                    "service": "Новый сервис", "env_name": "CUSTOM_API_KEY", "value": clip,
                    "category": "ai", "provider_url": "", "base_url": "", "detected_type": "Токен из буфера"
                }
            self._send_json({"ok": True, "candidate": cand})

        elif self.path == "/api/setx":
            env_name = body.get("env_name", "")
            value = body.get("value", "")
            if sys.platform == "win32" and env_name:
                subprocess.run(["setx", env_name, value], creationflags=subprocess.CREATE_NO_WINDOW)
                self._send_json({"ok": True, "msg": f"Переменная {env_name} записана в Windows ENV"})
            else:
                self._send_json({"ok": False, "msg": "Доступно только в ОС Windows"})

        elif self.path == "/api/ping":
            key_id = body.get("id")
            k = next((x for x in VAULT.data.get("keys", []) if x["id"] == key_id), None)
            if not k:
                self._send_json({"ok": False, "msg": "Ключ не найден"})
            else:
                res = check_key_health(k)
                self._send_json(res)

        elif self.path == "/api/launch_app":
            kind, target = resolve_launch_target(body.get("path", ""))
            if kind == "empty":
                self._send_json({"ok": False, "msg": "Путь не указан"})
                return
            # ключи агента -> переменные окружения для запускаемого процесса
            env = os.environ.copy()
            app_id = body.get("app_id")
            agent = next((a for a in VAULT.data.get("apps", []) if a.get("id") == app_id), None)
            linked = []
            if agent:
                for kid in agent.get("linked_keys", []):
                    k = next((x for x in VAULT.data.get("keys", []) if x.get("id") == kid), None)
                    if k:
                        linked.append(k)
            resolved = resolve_provider_env(linked)
            env.update(resolved)
            extra = (" · переменных передано: %d" % len(resolved)) if resolved else ""
            try:
                if kind == "file":
                    lower = target.lower()
                    if lower.endswith(".url"):
                        os.startfile(target)
                    elif lower.endswith(".lnk"):
                        subprocess.Popen(["cmd", "/c", "start", "", target], cwd=os.path.dirname(target) or BASE_DIR, env=env)
                    elif lower.endswith((".bat", ".cmd")):
                        subprocess.Popen(["cmd", "/c", target], cwd=os.path.dirname(target) or BASE_DIR, env=env)
                    else:
                        subprocess.Popen([target], cwd=os.path.dirname(target) or BASE_DIR, env=env)
                    self._send_json({"ok": True, "msg": f"Запущено: {target}{extra}"})
                elif kind == "dir":
                    os.startfile(target)
                    self._send_json({"ok": True, "msg": f"Открыта папка: {target}"})
                else:
                    parts = target.split()
                    exe = parts[0] if parts else target
                    if not os.path.isabs(exe) and not shutil.which(exe):
                        self._send_json({"ok": False, "msg": f"Команда не найдена: {exe}"})
                        return
                    subprocess.Popen(target, shell=True, cwd=BASE_DIR, env=env)
                    self._send_json({"ok": True, "msg": f"Запущено: {target}{extra}"})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"Ошибка запуска: {e}"})
        elif self.path == "/api/agent_icon":
            kind, target = resolve_launch_target(body.get("path", ""))
            icon = None
            if kind == "file" and target.lower().endswith(".exe"):
                icon = extract_exe_icon_png(target)
            self._send_json({"ok": bool(icon), "icon": icon, "target": target})

        elif self.path == "/api/open_url":
            url = body.get("url", "").strip()
            if url:
                open_in_browser(url)
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False})

        elif self.path == "/api/toggle_autostart":
            enabled = bool(body.get("enabled", False))
            ok = set_autostart(enabled)
            self._send_json({"ok": ok, "enabled": is_autostart_enabled() if ok else enabled})

        elif self.path == "/api/open_note":
            key_id = body.get("id")
            item = next((x for x in VAULT.data.get("personal", []) if x.get("id") == key_id), None)
            if not item:
                self._send_json({"ok": False, "msg": "Заметка не найдена"})
                return
            try:
                notes_dir = os.path.join(BASE_DIR, "notes")
                os.makedirs(notes_dir, exist_ok=True)
                safe = "".join(c for c in str(key_id) if c.isalnum() or c in "-_") or "note"
                note_path = os.path.join(notes_dir, safe + ".txt")
                with open(note_path, "w", encoding="utf-8-sig") as f:
                    f.write(item.get("notes", "") or "")
                notepad = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "notepad.exe")
                if os.path.exists(notepad):
                    subprocess.Popen([notepad, note_path])
                else:
                    os.startfile(note_path)
                self._send_json({"ok": True, "msg": "Открыто в блокноте"})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"Ошибка: {e}"})

        elif self.path == "/api/sync_note":
            key_id = body.get("id")
            item = next((x for x in VAULT.data.get("personal", []) if x.get("id") == key_id), None)
            if not item:
                self._send_json({"ok": False, "msg": "Заметка не найдена"})
                return
            try:
                notes_dir = os.path.join(BASE_DIR, "notes")
                safe = "".join(c for c in str(key_id) if c.isalnum() or c in "-_") or "note"
                note_path = os.path.join(notes_dir, safe + ".txt")
                if not os.path.exists(note_path):
                    self._send_json({"ok": False, "msg": "Файл заметки не найден, откройте заметку в блокноте"})
                    return
                with open(note_path, "r", encoding="utf-8-sig", errors="replace") as f:
                    item["notes"] = f.read()
                VAULT.save()
                self._send_json({"ok": True, "msg": "Заметка обновлена", "personal": VAULT.data.get("personal", [])})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"Ошибка: {e}"})

        elif self.path == "/api/set_primary":
            key_id = body.get("id")
            make_primary = bool(body.get("primary", True))
            keys = VAULT.data.get("keys", [])
            target = next((k for k in keys if k.get("id") == key_id), None)
            if not target:
                self._send_json({"ok": False, "msg": "Ключ не найден"})
                return
            sig = provider_signature(target)
            for k in keys:
                if provider_signature(k) == sig:
                    k["primary"] = False
            target["primary"] = make_primary
            VAULT.save()
            self._send_json({"ok": True, "keys": keys, "primary": make_primary})

        elif self.path == "/api/set_rotation":
            key_id = body.get("id")
            rotation = bool(body.get("rotation", True))
            keys = VAULT.data.get("keys", [])
            target = next((k for k in keys if k.get("id") == key_id), None)
            if not target:
                self._send_json({"ok": False, "msg": "Ключ не найден"})
                return
            sig = provider_signature(target)
            for k in keys:
                if provider_signature(k) == sig:
                    k["rotation"] = rotation
            VAULT.save()
            self._send_json({"ok": True, "keys": keys, "rotation": rotation})

        elif self.path == "/api/import_env_scan":
            folder = (body.get("folder") or "").strip()
            if not folder:
                self._send_json({"ok": False, "msg": "Укажите папку проекта"})
                return
            folder = os.path.abspath(os.path.expanduser(folder))
            if not os.path.isdir(folder):
                self._send_json({"ok": False, "msg": "Папка не найдена"})
                return
            items = scan_env_folder(folder)
            self._send_json({"ok": True, "folder": folder, "items": items})

        elif self.path == "/api/import_env_save":
            items = body.get("items") or []
            keys = VAULT.data.get("keys", [])
            taken = set((k.get("env_name") or "").lower() for k in keys)
            added = 0
            for it in items:
                env_name = (it.get("env_name") or "").strip()
                value = (it.get("value") or "").strip()
                if not env_name or not value:
                    continue
                if env_name.lower() in taken:
                    continue
                taken.add(env_name.lower())
                hint = it.get("hint") or {}
                keys.append({
                    "id": "k_%d_%d" % (int(time.time() * 1000), added),
                    "service": (it.get("service") or hint.get("service") or env_name).strip(),
                    "env_name": env_name,
                    "value": value,
                    "category": it.get("category") or hint.get("category") or "ai",
                    "provider_url": it.get("provider_url") or hint.get("provider_url") or "",
                    "base_url": it.get("base_url") or hint.get("base_url") or "",
                    "project": it.get("project") or "",
                    "notes": it.get("notes") or ("Импорт из " + (it.get("file") or ".env")),
                })
                added += 1
            VAULT.data["keys"] = keys
            VAULT.save()
            self._send_json({"ok": True, "added": added, "keys": keys})

        elif self.path == "/api/export_env_file":
            folder = body.get("folder") or BASE_DIR
            content = body.get("content", "")
            try:
                base = os.path.abspath(BASE_DIR)
                target_dir = os.path.abspath(os.path.expanduser(folder.strip()))
                try:
                    inside = os.path.commonpath([base, target_dir]) == base
                except ValueError:
                    inside = False
                if not inside:
                    self._send_json({"ok": False, "msg": "Путь вне папки проекта запрещен"})
                    return
                os.makedirs(target_dir, exist_ok=True)
                target_path = os.path.join(target_dir, ".env")
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(content + "\n")
                self._send_json({"ok": True, "msg": f"Сохранено: {target_path}"})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"Ошибка записи: {e}"})
        else:
            self.send_error(404)


def find_free_port(default_port=17555) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", default_port)) != 0:
            return default_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_window():
    if not SERVER_URL:
        return
    try:
        _open_window()
    except Exception as e:
        app_log("Не удалось открыть окно: %r" % (e,))


def open_in_browser(url):
    """Открывает ссылку в фоне: webbrowser.open на этой Windows умеет висеть минутами."""
    def worker():
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            app_log("Не удалось открыть ссылку %s: %r" % (url, e))
    threading.Thread(target=worker, daemon=True).start()


def _open_window():
    if sys.platform == "win32":
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for path in edge_paths:
            if os.path.exists(path):
                subprocess.Popen([path, f"--app={SERVER_URL}", "--window-size=1180,760"])
                return
    open_in_browser(SERVER_URL)


def create_colored_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(12, 19, 23, 255), outline=(53, 92, 105, 255), width=2)
    amber = (245, 158, 11, 255)
    d.line([(4, 18), (4, 6), (18, 6)], fill=amber, width=3)
    d.line([(46, 6), (60, 6), (60, 18)], fill=amber, width=3)
    d.line([(4, 46), (4, 60), (18, 60)], fill=amber, width=3)
    d.line([(46, 60), (60, 60), (60, 46)], fill=amber, width=3)
    d.polygon([(32, 11), (53, 32), (32, 53), (11, 32)], fill=(53, 92, 105, 255), outline=(34, 211, 238, 255))
    d.polygon([(32, 21), (43, 32), (32, 43), (21, 32)], fill=amber)
    return img


def setup_logging():
    """Логи pystray и приложения в app.log рядом с программой."""
    try:
        import logging
        handler = logging.FileHandler(os.path.join(BASE_DIR, "app.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        for name in ("pystray", "pystray._win32"):
            log = logging.getLogger(name)
            log.setLevel(logging.DEBUG)
            log.addHandler(handler)
    except Exception:
        pass


def app_log(message):
    try:
        with open(os.path.join(BASE_DIR, "app.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def setup_tray_and_run(server: HTTPServer):
    global TRAY_ICON
    setup_logging()
    if not pystray:
        open_window()
        server.serve_forever()
        return

    def on_open(icon, item):
        open_window()

    def on_grab_clipboard(icon, item):
        if pyperclip:
            clip = pyperclip.paste().strip()
            cand = parse_clipboard_candidate(clip)
            if not cand and clip:
                cand = {
                    "service": "Новый сервис", "env_name": "CUSTOM_API_KEY", "value": clip,
                    "category": "ai", "provider_url": "", "base_url": "", "detected_type": "Из буфера"
                }
            if cand:
                DETECTED_KEY_QUEUE.append(cand)
        open_window()

    def on_exit(icon, item):
        icon.stop()
        server.shutdown()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("◈ Открыть Cyber-Vault", on_open, default=True),
        pystray.MenuItem("📥 Захватить ключ из буфера", on_grab_clipboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("✕ Выход", on_exit)
    )

    TRAY_ICON = pystray.Icon("cyber_vault", create_colored_tray_image(), "CYBER-VAULT", menu)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Timer(0.5, open_window).start()

    # pystray: если цикл сообщений упал, он молча возвращает управление и процесс умирает.
    # Перезапускаем иконку, а в крайнем случае оставляем жить только веб-интерфейс.
    attempts = 0
    while True:
        try:
            TRAY_ICON.run()
        except Exception as e:
            app_log("Ошибка цикла трея: %r" % (e,))
        attempts += 1
        if attempts >= 3:
            app_log("Цикл трея умер окончательно - веб-интерфейс продолжает работать")
            break
        app_log("Цикл трея завершился, пересоздаём иконку (попытка %d)" % attempts)
        time.sleep(2)
        try:
            TRAY_ICON = pystray.Icon("cyber_vault", create_colored_tray_image(), "CYBER-VAULT", menu)
        except Exception as e:
            app_log("Не удалось пересоздать иконку трея: %r" % (e,))
            break

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", 17555)) == 0:
            SERVER_URL = "http://127.0.0.1:17555"
            open_window()
            sys.exit(0)

    port = find_free_port(17555)
    SERVER_URL = f"http://127.0.0.1:{port}"
    server = VaultServer(("127.0.0.1", port), VaultRequestHandler)

    clip_thread = threading.Thread(target=clipboard_watcher_loop, daemon=True)
    clip_thread.start()

    setup_tray_and_run(server)
