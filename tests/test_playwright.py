import os
from playwright.sync_api import sync_playwright, Page, BrowserContext

def _get_browser_context(playwright):
    # 可选：环境变量自定义反检测参数
    browser_lang = os.getenv("BROWSER_LANGUAGE", "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6")
    browser_ua = os.getenv("BROWSER_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0")
    window_size = os.getenv("BROWSER_WINDOW_SIZE", "1158,848")
    width, height = map(int, window_size.split(','))

    launch_options = {
        "headless": False,
        "args": [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
    }
    
    # 如果在 Docker 中，使用系统安装的 Chromium
    # if 'PYTHON_IN_DOCKER' in os.environ:
    launch_options["executable_path"] = "/usr/bin/chromium"

    browser = playwright.chromium.launch(**launch_options)
    
    context = browser.new_context(
        user_agent=browser_ua,
        viewport={'width': width, 'height': height},
        locale=browser_lang.split(',')[0],
        accept_downloads=True
    )
    
    # 注入脚本隐藏自动化特征
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)
    
    return browser, context
def test_playwright():
    with sync_playwright() as playwright:
        browser, context = _get_browser_context(playwright)
        page = context.new_page()
        page.goto("https://arh.antoinevastel.com/bots/areyouheadless")