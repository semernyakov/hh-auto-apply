#!/usr/bin/env python3
"""
HH.ru Авторизация
Сохраняет сессию браузера для автооткликатора
"""

import os
import sys
import json
import time
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

N8N_FILES_DIR = os.getenv("N8N_FILES_DIR", os.path.expanduser("~/.n8n-files"))
SESSION_FILE = os.path.join(N8N_FILES_DIR, "hh_session.json")

def ensure_dir():
    if not os.path.exists(N8N_FILES_DIR):
        print(f"📁 Создаю папку: {N8N_FILES_DIR}")
        os.makedirs(N8N_FILES_DIR)

def login():
    ensure_dir()
    
    print("\n" + "="*50)
    print("🔐 HH.ru Авторизация")
    print("="*50)
    
    # Системный Chromium, sandbox-флаги и прокси — как у ботов.
    from auto_apply_template import launch_browser
    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)
        context = browser.new_context()

        page = context.new_page()
        page.goto("https://hh.ru/login", timeout=120000, wait_until="domcontentloaded")

        print("\n📌 Войди в свой аккаунт HH.ru в открывшемся браузере.")
        print("⏳ Сессия сохранится сама, как только вход завершится (до 10 минут).")

        # Через `!` в Claude Code нет stdin — ждём входа по страницам, а не по Enter.
        # Состояние запоминаем на каждом шаге: если окно закроют, сохраним последнее.
        logged, last_state = False, None
        deadline = time.time() + 600
        while time.time() < deadline and not logged:
            time.sleep(3)
            try:
                pages = [pg for pg in context.pages if not pg.is_closed()]
                if not pages:
                    break
                last_state = context.storage_state()
                for pg in pages:
                    path = pg.url.split("?")[0]
                    if "hh.ru" not in path or "login" in path or "/account/" in path:
                        continue
                    # В шапке залогиненного HH бывают скрытые «Войти» — считаем только видимые.
                    guest = pg.locator("header a:has-text('Войти'), header button:has-text('Войти')")
                    if pg.locator("header").count() and guest.filter(visible=True).count() == 0:
                        logged = True
                        break
            except Exception:
                continue

        if logged:
            time.sleep(3)
            context.storage_state(path=SESSION_FILE)
        elif last_state:
            with open(SESSION_FILE, "w") as f:
                json.dump(last_state, f)
            print("\n⚠️ Вход не распознан — сохранено последнее состояние браузера, проверьте ботом.")
        else:
            print("\n❌ Сессия не сохранена.")
            sys.exit(1)

        print(f"\n✅ Сессия сохранена в {SESSION_FILE}")
        print("🎉 Теперь можешь запускать auto_apply.py!")
        
        try:
            browser.close()
        except Exception:
            pass

def get_cookies():
    if not os.path.exists(SESSION_FILE):
        print("❌ Сессия не найдена. Сначала запусти login.")
        return None
    
    with open(SESSION_FILE, 'r') as f:
        state = json.load(f)
        cookies = state.get('cookies', [])
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        return cookie_str

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--get-cookies":
        print(get_cookies())
    else:
        login()
