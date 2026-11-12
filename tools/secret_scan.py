# -*- coding: utf-8 -*-
"""Секрет-скан по дереву проекта: ищем паттерны настоящих ключей."""
import io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"libs", "backup", "notes", ".git", "__pycache__", "_audit_tmp", ".tmp"}
SKIP_FILES = {"vault.json", "app.log", "icon_debug.log"}   # игнорируются: вне git

PATTERNS = [
    ("OpenRouter (реальный)", r"sk-or-v1-[a-fA-F0-9]{64}"),
    ("OpenRouter (любой)", r"sk-or-v1-[A-Za-z0-9_-]{20,}"),
    ("Gemini", r"AIza[0-9A-Za-z_-]{35}"),
    ("Groq", r"gsk_[a-zA-Z0-9]{48,56}"),
    ("GitHub classic", r"ghp_[a-zA-Z0-9]{36}"),
    ("GitHub fine-grained", r"github_pat_[a-zA-Z0-9_]{82}"),
    ("HuggingFace", r"hf_[a-zA-Z0-9]{34}"),
    ("Telegram bot", r"\d{8,11}:AA[A-Za-z0-9_-]{33}"),
    ("OpenAI", r"sk-[A-Za-z0-9]{48}"),
    ("Anthropic", r"sk-ant-[A-Za-z0-9_-]{90,}"),
    ("aws access key", r"AKIA[0-9A-Z]{16}"),
    ("Twogp/live (ogw)", r"ogw_live_[A-Za-z0-9]{16,}"),
    ("ak_ (llm-chat)", r"ak_[A-Za-z0-9]{10,}"),
    ("sk-spc (spc)", r"sk-spc[A-Za-z0-9]{10,}"),
    ("sk-at (apex)", r"sk-at-[A-Za-z0-9-]{10,}"),
    ("sk-apx", r"sk-apx[A-Za-z0-9]{10,}"),
    ("sk-dc9", r"sk-dc9[A-Za-z0-9]{10,}"),
    ("sk-any", r"sk-any[A-Za-z0-9]{10,}"),
    ("Google OAuth", r"[0-9]+-[a-z0-9_]{32}\.apps\.googleusercontent\.com"),
    ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]

hits = []
scanned = 0
for root, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
    for name in files:
        if name in SKIP_FILES or name.endswith((".png", ".pyc", ".ico", ".exe", ".dll", ".zip")):
            continue
        p = os.path.join(root, name)
        try:
            text = io.open(p, encoding="utf-8").read()
        except Exception:
            continue
        scanned += 1
        rel = os.path.relpath(p, ROOT)
        for label, pat in PATTERNS:
            for m in re.finditer(pat, text):
                line_no = text.count("\n", 0, m.start()) + 1
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.start())
                line_text = text[start:end if end > 0 else len(text)]
                if "demo" in line_text.lower() or "not_a_real" in line_text.lower():
                    continue          # заведомо демонстрационное значение
                hits.append((label, rel, line_no, m.group(0)[:24]))

print("Просканировано файлов: %d" % scanned)
if not hits:
    print("СОВПАДЕНИЙ НЕТ — дерево чистое")
    sys.exit(0)

print("Совпадений: %d" % len(hits))
for label, rel, line, snippet in hits:
    print("  %-22s %s:%d  %s..." % (label, rel, line, snippet))
sys.exit(1)
