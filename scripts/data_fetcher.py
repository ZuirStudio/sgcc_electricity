import logging
import os
import re
import time
import json

import random
import base64
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright_stealth import Stealth
from sensor_updator import SensorUpdator
from error_watcher import ErrorWatcher
from typing import Optional, List, Tuple

from const import *

import numpy as np
from captcha_playwright import solve_captcha_in_browser
import vue_state

class DataFetcher:

    def __init__(self, username: str, password: str):
        if 'PYTHON_IN_DOCKER' not in os.environ:
            import dotenv
            dotenv.load_dotenv(verbose=True)
        self._username = username
        self._password = password

        self.DRIVER_IMPLICITY_WAIT_TIME = int(os.getenv("DRIVER_IMPLICITY_WAIT_TIME", 60))
        self.RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))
        self.LOGIN_EXPECTED_TIME = int(os.getenv("LOGIN_EXPECTED_TIME", 10))
        self.RETRY_WAIT_TIME_OFFSET_UNIT = int(os.getenv("RETRY_WAIT_TIME_OFFSET_UNIT", 10))
        self.IGNORE_USER_ID = os.getenv("IGNORE_USER_ID", "xxxxx,xxxxx").split(",")
        self.QR_CODE_LOGIN_WAIT_COUNT = int(os.getenv("QR_CODE_LOGIN_WAIT_COUNT", 7))
        self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT = int(os.getenv("QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT", 10))
        self.HEADLESS_NEW = os.getenv("HEADLESS_NEW", "true").lower() == "true" or 'PYTHON_IN_DOCKER' in os.environ
        self._user_name_map = {}
        raw_names = os.getenv("USER_NAMES", "")
        if raw_names:
            for pair in raw_names.split(","):
                if ":" in pair:
                    uid, name = pair.split(":", 1)
                    self._user_name_map[uid.strip()] = name.strip()
        self._init_db()

    def _init_db(self):
        self.db_type = os.getenv("DB_TYPE", "None").lower()
        if self.db_type == 'mysql':
            from db import MysqlDB
            self.db = MysqlDB()
            logging.info("使用 MySQL 数据库存储数据。")
        elif self.db_type == 'sqlite':
            from db import SqliteDB
            self.db = SqliteDB()
            logging.info("使用 SQLite 数据库存储数据。")
        else:
            self.db = None
            logging.info("不使用数据库存储数据。")

    # @staticmethod
    def _click_button(self, page: Page, selector: str):
        '''封装点击函数，仅在元素可点击时点击'''
        page.wait_for_selector(selector, state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)
        page.click(selector)
        # 点击后添加微小随机暂停，模拟人工操作
        time.sleep(random.uniform(0.1, 0.5))

    def insert_expand_data(self, data:dict):
        self.db.insert_expand_data(data)

    def _find_browser_executable(self) -> Optional[str]:
        """尝试在不同环境中寻找可用的浏览器可执行文件。"""
        # 1) 优先检查环境变量
        env_path = os.getenv("BROWSER_EXECUTABLE_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        # 2) Docker 环境
        if 'PYTHON_IN_DOCKER' in os.environ:
            if os.path.exists("/usr/bin/chromium"):
                return "/usr/bin/chromium"
            if os.path.exists("/usr/bin/chromium-browser"):
                return "/usr/bin/chromium-browser"

        # 3) 尝试从系统 PATH 中查找
        import shutil
        browsers = ["chromium", "google-chrome", "chrome", "msedge"]
        if os.name == 'nt':
            browsers = [b + ".exe" for b in browsers]
        
        for b in browsers:
            path = shutil.which(b)
            if path:
                return path

        # 4) Windows 常见安装路径
        if os.name == 'nt':
            program_files = [os.environ.get("ProgramFiles", "C:\\Program Files"), 
                             os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
                             os.environ.get("LocalAppData", "")]
            
            suffixes = [
                "Google\\Chrome\\Application\\chrome.exe",
                "Microsoft\\Edge\\Application\\msedge.exe",
                "Chromium\\Application\\chrome.exe"
            ]
            
            for base in program_files:
                if not base: continue
                for suffix in suffixes:
                    full_path = os.path.join(base, suffix)
                    if os.path.exists(full_path):
                        return full_path

        # 5) 返回 None，让 Playwright 尝试使用自带的浏览器（如果已安装）
        return None

    def _get_browser_context(self, playwright):
        # 可选：环境变量自定义反检测参数
        browser_lang = os.getenv("BROWSER_LANGUAGE", "zh-HK,zh,en-US,en")
        browser_ua = os.getenv("BROWSER_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        window_size = os.getenv("BROWSER_WINDOW_SIZE", "1158,848")
        width, height = map(int, window_size.split(','))

        launch_options = {
            "headless": self.HEADLESS_NEW,
            "args": [
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        }
        
        exec_path = self._find_browser_executable()
        if exec_path:
            launch_options["executable_path"] = exec_path
            logging.info(f"使用浏览器路径: {exec_path}")
        else:
            logging.info("未找到系统浏览器，将尝试使用 Playwright 内置浏览器。")

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

    @ErrorWatcher.watch
    def _login(self, page: Page, phone_code = False):
        try:
            page.goto(LOGIN_URL)
            page.wait_for_selector(".user", state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 3000)
        except Exception:
            logging.error(f"登录页面加载失败: {LOGIN_URL}")
            return False
        logging.info(f"打开登录页面: {LOGIN_URL}。\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT*2)
        
        # swtich to username-password login page
        try:
            page.wait_for_selector('.el-loading-mask', state="hidden", timeout=10000)
        except Exception:
            pass

        page.click(".user")
        logging.info("已找到 'user'元素。\r")
        self._click_button(page, '//*[@id="login_box"]/div[1]/div[1]/div[2]/span')
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 点击同意按钮
        self._click_button(page, '//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]')
        logging.info("已点击同意选项。\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        if phone_code:
            self._click_button(page, '//*[@id="login_box"]/div[1]/div[1]/div[3]/span')
            input_elements = page.query_selector_all(".el-input__inner")
            input_elements[2].fill(self._username)
            logging.info(f"已输入用户名: {self._username}\r")
            self._click_button(page, '//*[@id="login_box"]/div[2]/div[2]/form/div[1]/div[2]/div[2]/div/a')
            code = input("请输入手机验证码: ")
            input_elements[3].fill(code)
            logging.info(f"已输入验证码: {code}。\r")
            # 点击登录按钮
            self._click_button(page, '//*[@id="login_box"]/div[2]/div[2]/form/div[2]/div/button/span')
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT*2)
            logging.info("已点击登录按钮。\r")

            return True
        # 增加判空校验便于测试备用方案
        elif self._password is not None and len(self._password) > 0:
            # 输入用户名和密码
            input_elements = page.query_selector_all(".el-input__inner")
            input_elements[0].fill(self._username)
            logging.info(f"已输入用户名: {self._username}\r")
            input_elements[1].fill(self._password)
            logging.info(f"已输入密码: {self._password}\r")

            # 点击登录按钮
            self._click_button(page, ".el-button.el-button--primary")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            logging.info("已点击登录按钮。\r")

            # 快速检查：如果已经跳转离开登录页，说明无需验证码，直接成功
            if page.url != LOGIN_URL:
                logging.info("无需验证码登录成功 (已被重定向)。\r")
                return True

            # 会出现点击登录直接失败（账号被限制登录）
            error = self._get_error_message(page, "//div[@class='errmsg-tip']//span")
            if error is None:
                # 处理腾讯点击验证码
                captcha_passed = solve_captcha_in_browser(page, max_retries=self.RETRY_TIMES_LIMIT)
                if captcha_passed:
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    if page.url != LOGIN_URL:
                        logging.info("通过点击验证码登录成功。\r")
                        return True
                    else:
                        error = self._get_error_message(page, "//div[@class='errmsg-tip']//span")
                        if error:
                            logging.info(f"验证码通过但登录失败: [{error}]\r")
                        else:
                            logging.error("验证码已通过但仍停留在登录页面。")
                else:
                    error = self._get_error_message(page, "//div[@class='errmsg-tip']//span")
                    logging.error("点击验证码识别在所有重试后均失败。")
            else:
                logging.error(f"登录失败: [{error}]\r")    
        return self._fallback_login(page, error)

    def _get_error_message(self, page: Page, path) -> Optional[str]:
        """获取错误信息，如果不存在则返回 None"""
        try:
            element = page.locator(f"xpath={path}")
            if element.is_visible(timeout=2000):
                return element.inner_text()
            return None
        except Exception:
            return None

    def _fallback_login(self, page: Page, reason: str) -> bool:
        """使用备用方案登录"""
        fallback = os.getenv("LOGIN_FALLBACK")
        if fallback == 'qrcode':
            return self._qr_login(page, reason)
        return False

    def _qr_login(self, page: Page, reason: str) -> bool:
        logging.info("二维码登录开始")
        # 切换验证码
        page.wait_for_selector('.qr_code', state="attached", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)
        page.evaluate("document.querySelector('.qr_code').click()")
        logging.info("已切换到二维码模式")

        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 获取登录二维码
        qr_selector = "//div[@class='sweepCodePic']//img"
        page.wait_for_selector(f"xpath={qr_selector}", state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)
        logging.info("已找到二维码图片元素")

        qr_element = page.locator(f"xpath={qr_selector}")
        img_src = qr_element.get_attribute('src')

        if img_src.startswith('data:image'):
            base64_data = img_src.split(',')[1]
            img_screenshot = base64.b64decode(base64_data)
        else:
          logging.info('二维码图片源不是 base64 格式')
          img_screenshot = qr_element.screenshot()

        with open("/data/login_qr_code.png", "wb") as f:
            f.write(img_screenshot)
            logging.info("已将二维码保存到 /data/login_qr_code.png")

        from notify import UrlLoginQrCodeNotify
        notifyFunc = UrlLoginQrCodeNotify()
        notifyFunc(img_screenshot, reason)
        for i in range(1, self.QR_CODE_LOGIN_WAIT_COUNT + 1):
            logging.info(f'二维码登录等待检查[{self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT}] 次数[{i}]')
            time.sleep(self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT)
            if (page.url != LOGIN_URL):
                logging.info("二维码登录成功")
                return True
            else:
                qr_error = self._get_error_message(page, "//div[@class='sweepCodePic']//div[@class='erwBg']//p")
                if qr_error is not None:
                    logging.error(f'二维码登录错误[{qr_error}]')
                    return False

        logging.warning("二维码登录超时")

        return False

    def _random_delay(self, min_seconds=0.5, max_seconds=3.0):
        """添加随机延迟，使自动化操作更难被检测。"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)


    def fetch(self):

        """主逻辑"""
        with Stealth().use_sync(sync_playwright()) as playwright:
            browser, context = self._get_browser_context(playwright)
            page = context.new_page()
            
            # ErrorWatcher 需要适配 Playwright，这里暂时传 page
            ErrorWatcher.instance().set_driver(page)

            self._random_delay(1, 3)
            logging.info("浏览器驱动已初始化。")
            updator = SensorUpdator()

            try:
                if os.getenv("DEBUG_MODE", "false").lower() == "true":
                    if self._login(page, phone_code=True):
                        logging.info("登录成功!")
                    else:
                        logging.info("登录失败!")
                        raise Exception("login unsuccessed")
                else:
                    if self._login(page):
                        logging.info("登录成功!")
                    else:
                        logging.info("登录失败!")
                        raise Exception("login unsuccessed")
            except Exception as e:
                logging.error(
                    f"浏览器驱动异常退出，原因: {e}。还剩 {self.RETRY_TIMES_LIMIT} 次重试机会。")
                browser.close()
                return

            logging.info(f"在 {LOGIN_URL} 登录成功")
            self._random_delay(1, 3)
            logging.info(f"尝试获取用户 ID 列表")
            user_id_list = self._get_user_ids(page)
            logging.info(f"共找到 {len(user_id_list)} 个用户 ID，其中 {user_id_list} 将被忽略: {self.IGNORE_USER_ID}")
            self._random_delay(0.5, 2)


            for userid_index, user_id in enumerate(user_id_list):
                try:
                    self._random_delay(1, 3)
                    # 切换到电费余额页面
                    page.goto(BALANCE_URL)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    self._choose_current_userid(page, userid_index)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    current_userid = self._get_current_userid(page)
                    if current_userid in self.IGNORE_USER_ID:
                        logging.info(f"用户 ID {current_userid} 将被忽略")
                        continue
                    else:
                        ### 获取数据
                        balance, last_daily_date, last_daily_usage, yearly_charge, yearly_usage, month_charge, month_usage, tou_data, enhanced_balance = self._get_all_data(page, user_id, userid_index)
                        logging.info(f"用户 [{user_id}] 数据获取完成: 余额={balance}元, 最近日用电={last_daily_usage}度({last_daily_date}), "
                                     f"年度用电={yearly_usage}度, 年度电费={yearly_charge}元, 月用电={month_usage}度, 月电费={month_charge}元")
                        updator.update_one_userid(user_id, balance, last_daily_date, last_daily_usage, yearly_charge, yearly_usage, month_charge, month_usage, tou_data=tou_data, enhanced_balance=enhanced_balance)

                        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                except Exception as e:
                    if (userid_index != len(user_id_list)):
                        logging.info(f"当前用户 {user_id} 数据抓取失败: {e}，将继续抓取下一个用户数据。")
                    else:
                        logging.info(f"用户 {user_id} 数据抓取失败: {e}")
                        logging.info("数据抓取完成后浏览器驱动退出。")
                    continue

            browser.close()


    def _get_current_userid(self, page: Page) -> str:
        """读取当前页面的用户户号（兼容多种页面布局）"""
        # 方式一：从"用电户号"标签中读取
        try:
            label = page.locator("//*[contains(normalize-space(.), '用电户号')]").first.inner_text() or ""
            matches = re.findall(r"\b\d{13}\b", label)
            if matches:
                return matches[-1]
        except Exception:
            pass
        # 方式二：从页面源码中正则匹配
        try:
            page_content = page.content() or ""
            match = re.search(r"用电户号[:：\s]*([0-9]{13})", page_content)
            if match:
                return match.group(1)
        except Exception:
            pass
        # 方式三：从下拉框中读取当前选中项
        try:
            dropdown = page.locator(".el-dropdown")
            text = dropdown.inner_text() or ""
            matches = re.findall(r"\b\d{13}\b", text)
            if matches:
                return matches[-1]
        except Exception:
            pass
        logging.warning("无法读取当前户号")
        return ""

    def _choose_current_userid(self, page: Page, userid_index):
        """切换到指定索引的用户户号"""
        # 关闭确认弹窗（如果有）
        elements = page.query_selector_all(".button_confirm")
        if elements:
            try:
                page.click("//*[@id='app']/div/div[2]/div/div/div/div[2]/div[2]/div/button", timeout=2000)
            except Exception:
                pass
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

        # 打开用户选择器（兼容多种触发方式）
        try:
            trigger_selector = (
                "//span[contains(normalize-space(.), '切换用户')]"
                " | //div[contains(@class,'houseNum')]//div[contains(@class,'el-select')]//span[contains(@class,'el-input__suffix')]"
                " | //div[contains(@class,'houseNum')]//span[contains(normalize-space(.), '切换用户')]"
            )
            page.wait_for_selector(f"xpath={trigger_selector}", state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)
            page.click(f"xpath={trigger_selector}")
        except Exception:
            # 备用方案: 点击 el-input__suffix（下拉箭头）
            page.click(".el-input__suffix")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

        # 获取下拉选项并点击目标
        options = self._get_visible_user_options(page)
        if userid_index >= len(options):
            logging.error(f"用户索引 {userid_index} 超出范围, 共 {len(options)} 个选项")
            return
        options[userid_index].click()
        logging.info(f"已切换到用户索引 {userid_index}")

    def _get_visible_user_options(self, page: Page):
        """获取可见的用户下拉选项（兼容 el-dropdown 和 el-select）"""
        selectors = [
            "//ul[contains(@class,'el-dropdown-menu')]//li",
            "//div[contains(@class,'el-select-dropdown')]//li"
        ]
        options = []
        for selector in selectors:
            elements = page.query_selector_all(f"xpath={selector}")
            for el in elements:
                if el.is_visible() and "disabled" not in (el.get_attribute("class") or ""):
                    options.append(el)
        return options


    def _get_all_data(self, page: Page, user_id, userid_index):
        logging.info(f"[{user_id}] 正在获取电费余额...")
        balance = self._get_electric_balance(page)
        if balance is None:
            logging.error(f"[{user_id}] 获取电费余额失败")
        else:
            logging.info(f"[{user_id}] 电费余额: {balance} 元")

        # 尝试通过 Vue state 获取增强余额
        enhanced_balance = None
        user_name = self._user_name_map.get(user_id, "")
        if user_name:
            logging.info(f"[{user_id}] 用户名: {user_name}")
        if self.db is not None:
            try:
                components = vue_state.selected_vue_data(page)
                enhanced_balance = vue_state.normalize_balance(components)
            except Exception as e:
                logging.warning(f"[{user_id}] 增强余额获取失败: {e}")

        logging.info(f"[{user_id}] 正在切换到用电量页面...")
        page.goto(ELECTRIC_USAGE_URL)
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        try:
            self._choose_current_userid(page, userid_index)
        except Exception as e:
            logging.warning(f"[{user_id}] 用电量页面用户切换失败 (非致命): {e}")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

        logging.info(f"[{user_id}] 正在获取年度用电数据...")
        yearly_usage, yearly_charge = self._get_yearly_data(page)
        if yearly_usage is None:
            logging.error(f"[{user_id}] 获取年度用电量失败")
        else:
            logging.info(f"[{user_id}] 年度用电量: {yearly_usage} 度")
        if yearly_charge is None:
            logging.error(f"[{user_id}] 获取年度电费失败")
        else:
            logging.info(f"[{user_id}] 年度电费: {yearly_charge} 元")

        logging.info(f"[{user_id}] 正在获取月度用电数据...")
        month, month_usage, month_charge = self._get_month_usage(page)
        if month is None:
            logging.error(f"[{user_id}] 获取月度用电数据失败")
        else:
            for m in range(len(month)):
                logging.info(f"[{user_id}] {month[m]}: 用电 {month_usage[m]} 度, 电费 {month_charge[m]} 元")

        logging.info(f"[{user_id}] 正在获取每日用电量...")
        last_daily_date, last_daily_usage = self._get_yesterday_usage(page)
        if last_daily_usage is None:
            logging.error(f"[{user_id}] 获取每日用电量失败")
        else:
            logging.info(f"[{user_id}] 最近用电: {last_daily_date} 用电 {last_daily_usage} 度")

        # 尝试通过 Vue state 获取分时电量
        tou_data = None
        if self.db is not None:
            try:
                components = vue_state.selected_vue_data(page)
                usage_info = vue_state.normalize_usage(components)
                tou_data = usage_info
                logging.info(f"[{user_id}] Vue state 分时数据: 年度={usage_info.get('year')}, "
                             f"月数据={len(usage_info.get('months', []))}条, "
                             f"日数据={len(usage_info.get('daily', []))}条")
                # 打印 Vue state 日数据详情
                if usage_info.get("daily"):
                    for d in usage_info["daily"][:7]:
                        logging.info(f"  [日数据] {d.get('date')}: "
                                     f"总={d.get('total_usage')}度, "
                                     f"谷={d.get('valley_usage')}, 平={d.get('flat_usage')}, "
                                     f"峰={d.get('peak_usage')}, 尖={d.get('tip_usage')}")
                    if len(usage_info["daily"]) > 7:
                        logging.info(f"  ... 还有 {len(usage_info['daily']) - 7} 条日数据")
            except Exception as e:
                logging.warning(f"[{user_id}] Vue state 分时数据获取失败: {e}")

        # 尝试获取电费账单明细（月度分时）
        bill_tou_data = None
        if self.db is not None:
            try:
                bill_tou_data = self._get_bill_detail(page, user_id)
            except Exception as e:
                logging.warning(f"[{user_id}] 电费账单分时数据获取失败: {e}")

        # 数据库存储
        if self.db is not None:
            logging.info(f"[{user_id}] 数据库类型: {self.db_type}, 开始保存数据到数据库")
            date_list, usage_list = self._get_daily_usage_data(page)
            self._save_user_data(
                user_id, balance, enhanced_balance,
                last_daily_date, last_daily_usage,
                date_list, usage_list,
                month, month_usage, month_charge,
                yearly_charge, yearly_usage,
                tou_data, bill_tou_data, user_name,
            )
        else:
            logging.info(f"[{user_id}] 未配置数据库, 跳过数据存储")

        if month_charge:
            month_charge = month_charge[-1]
        else:
            month_charge = None
        if month_usage:
            month_usage = month_usage[-1]
        else:
            month_usage = None

        return balance, last_daily_date, last_daily_usage, yearly_charge, yearly_usage, month_charge, month_usage, tou_data, enhanced_balance

    def _get_user_ids(self, page: Page):
        """获取用户 ID 列表。优先从 el-dropdown 获取（余额页面），
        失败则从 el-select 获取（用电量页面），最后从页面源码正则匹配。"""
        try:
            # 方式一：经典方式 - 从 el-dropdown 下拉框获取
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            dropdowns = page.query_selector_all('.el-dropdown')
            if dropdowns:
                self._click_button(page, "//div[@class='el-dropdown']/span")
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                try:
                    target_selector = ".el-dropdown-menu.el-popper li"
                    page.wait_for_selector(target_selector, state="visible", timeout=10000)
                    
                    # 等待文本包含 ":"
                    def check_text(p):
                        li = p.query_selector(target_selector)
                        return li and ":" in (li.inner_text() or "")
                    
                    try:
                        page.wait_for_function("() => { const li = document.querySelector('.el-dropdown-menu.el-popper li'); return li && li.innerText.includes(':'); }", timeout=10000)
                    except Exception:
                        pass
                    
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    userid_elements = page.query_selector_all(".el-dropdown-menu.el-popper li")
                    userid_list = []
                    for element in userid_elements:
                        matches = re.findall("[0-9]+", element.inner_text() or "")
                        if matches:
                            uid = matches[-1]
                            userid_list.append(uid)
                    if userid_list:
                        logging.info(f"从 el-dropdown 获取到 {len(userid_list)} 个用户: {userid_list}")
                        return userid_list
                except Exception as e:
                    logging.debug(f"el-dropdown 获取失败, 尝试其他方式: {e}")

            # 方式二：从 el-select 下拉框获取（用电量页面）
            try:
                select_inputs = page.query_selector_all(".houseNum .el-select .el-input__inner")
                if not select_inputs:
                    page.goto(ELECTRIC_USAGE_URL)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
                    select_inputs = page.query_selector_all(".houseNum .el-select .el-input__inner")

                if select_inputs:
                    select_inputs[0].click()
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

                    options = page.query_selector_all(".el-select-dropdown__item")
                    userid_list = []
                    for opt in options:
                        text = (opt.inner_text() or "").strip()
                        if re.match(r'^\d{4}$', text):
                            continue
                        opt.click()
                        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                        try:
                            current_id = self._get_current_userid(page)
                            if current_id and current_id not in userid_list:
                                userid_list.append(current_id)
                                logging.info(f"从 el-select 获取到用户: {current_id} ({text})")
                        except Exception:
                            pass
                        select_inputs = page.query_selector_all(".houseNum .el-select .el-input__inner")
                        if select_inputs:
                            select_inputs[0].click()
                            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

                    if userid_list:
                        logging.info(f"从 el-select 获取到 {len(userid_list)} 个用户: {userid_list}")
                        return userid_list
            except Exception as e:
                logging.debug(f"el-select 获取失败: {e}")

            # 方式三：从页面源码正则匹配所有13位户号
            page_content = page.content() or ""
            all_ids = list(set(re.findall(r'\b(\d{13})\b', page_content)))
            if all_ids:
                logging.info(f"从页面源码正则匹配到 {len(all_ids)} 个用户: {all_ids}")
                return all_ids

            logging.error("所有方式均未能获取用户 ID 列表")
            return []
        except Exception as e:
            logging.error(f"获取用户 ID 列表异常: {e}")
            return []

    def _get_electric_balance(self, page: Page):
        try:
            try:
                # 定位是否有"应交金额"标题（确认是后缴费账户）
                title_element = page.locator("//p[contains(@class, 'balance_title') and contains(text(), '应交金额')]")
                if title_element.is_visible(timeout=2000):
                    title_text = title_element.inner_text()
                    if "应交金额" in title_text:
                        # 后缴费账户：需要查找"账户余额"，而不是"应交金额"
                        # 查找包含"账户余额"的balance_title元素，然后获取其内部的金额
                        balance_content = page.locator("//p[contains(@class, 'balance_title') and contains(text(), '账户余额')]")
                        # 提取数字部分
                        balance_text = re.sub(r'[^\d.]', '', balance_content.inner_text())
                        if balance_text:
                            return float(balance_text)
            except Exception as e:
                # 后缴费账户解析失败，继续尝试预缴费账户逻辑
                pass

            # 2. 预缴费账户的"账户余额"（原逻辑）
            balance_element = page.locator(".cff8")
            balance_text = balance_element.inner_text()
            balance = balance_text.replace("元", "")
            if "欠费" in balance_text:
                return -float(balance)
            else:
                return float(balance)
        except Exception as e:
            logging.error(f"获取余额失败: {e}")
            return None

    def _get_yearly_data(self, page: Page):

        try:
            if datetime.now().month == 1:
                self._click_button(page, '//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input')
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                span_element = page.locator(f"//span[text() = '{datetime.now().year - 1}']")
                span_element.click()
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            self._click_button(page, "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # 等待数据显示
            target = page.locator(".total")
            target.wait_for(state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)
        except Exception as e:
            logging.error(f"年度数据获取失败: {e}")
            return None, None

        # 获取数据
        try:
            yearly_usage = page.locator("//ul[@class='total']/li[1]/span").inner_text()
        except Exception as e:
            logging.error(f"年度用电量数据获取失败: {e}")
            yearly_usage = None

        try:
            yearly_charge = page.locator("//ul[@class='total']/li[2]/span").inner_text()
        except Exception as e:
            logging.error(f"年度电费数据获取失败: {e}")
            yearly_charge = None

        return yearly_usage, yearly_charge

    def _get_yesterday_usage(self, page: Page):
        """获取最近一次用电量"""
        try:
            # 点击日用电量 tab
            self._click_button(page, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            # 等待数据表格出现（兼容多种滚动类名）
            usage_selector = """//*[@id="pane-second"]/div[2]/div[2]/div[1]/div[3]/table/tbody/tr[1]/td[2]/div"""
            usage_element = page.locator(f"xpath={usage_selector}")
            usage_element.wait_for(state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000) # 等待用电量出现

            # 增加是哪一天
            date_selector = """//*[@id="pane-second"]/div[2]/div[2]/div[1]/div[3]/table/tbody/tr[1]/td[1]/div"""
            date_element = page.locator(f"xpath={date_selector}")
            last_daily_date = date_element.inner_text() # 获取最近一次用电量的日期
            return last_daily_date, float(usage_element.inner_text())
        except Exception as e:
            logging.error(f"每日用电量数据获取失败: {e}")
            return None, None

    def _get_month_usage(self, page: Page):
        """获取每月用电量"""

        try:
            self._click_button(page, "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            if datetime.now().month == 1:
                self._click_button(page, '//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input')
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                span_element = page.locator(f"//span[text() = '{datetime.now().year - 1}']")
                span_element.click()
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # 等待月度数据出现
            target = page.locator(".total")
            target.wait_for(state="visible", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)
            month_element_text = page.locator("//*[@id='pane-first']/div[1]/div[2]/div[2]/div/div[3]/table/tbody").inner_text()
            month_element = month_element_text.split("\n")
            month_element = [x for x in month_element if x != "MAX"]
            if len(month_element) % 3 != 0:
                month_element = month_element[:-(len(month_element) % 3)]
            month_element = np.array(month_element).reshape(-1, 3)
            # 将每月的用电量保存为List
            month = []
            usage = []
            charge = []
            for i in range(len(month_element)):
                month.append(month_element[i][0])
                usage.append(month_element[i][1])
                charge.append(month_element[i][2])
            return month, usage, charge
        except Exception as e:
            logging.error(f"月度数据获取失败: {e}")
            return None,None,None

    # 增加获取每日用电量的函数
    def _get_daily_usage_data(self, page: Page):
        """获取每日用电量数据 (7天或30天)，通过 radio 按钮切换，失败时返回空列表"""
        try:
            fetch_days = int(os.getenv("DAILY_FETCH_DAYS", 7))
            if fetch_days not in (7, 30):
                fetch_days = 7
            logging.info(f"正在获取每日用电量数据 (最近 {fetch_days} 天)")
            # 点击"日用电量" tab
            self._click_button(page, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 3)

            # 通过 radio 按钮点击 7天 或 30天
            if fetch_days == 30:
                try:
                    radio_selector = (
                        "//span[contains(@class,'el-radio__label') and contains(text(),'近30天')]"
                        "/preceding-sibling::span//input[@class='el-radio__original']"
                    )
                    page.evaluate(f"document.evaluate(\"{radio_selector}\", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue.click()")
                    logging.info("已点击 '近30天' radio 按钮")
                except Exception:
                    try:
                        self._click_button(page, "//*[@id='pane-second']//label[2]//span[@class='el-radio__input']")
                        logging.info("已点击 '近30天' 备用方案")
                    except Exception:
                        logging.warning("未找到 '近30天' radio, 使用默认数据")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 3)

            # 原因分析：XPath 选择器匹配到了多个 tab-pane (有一个隐藏的)
            # Playwright 拿到了第一个元素，但它是隐藏的 (aria-hidden="true")
            # 解决方法：明确选择 id="pane-second" 且可见的 tab-pane
            usage_xpath = (
                "//div[@id='pane-second' and not(@aria-hidden='true')]"
                "//div[contains(@class,'el-table__body-wrapper')]"
                "//table/tbody/tr[1]/td[2]/div"
            )
            # 只检查是否存在于 DOM，不强制要求可见
            page.wait_for_selector(f"xpath={usage_xpath}", state="attached", timeout=self.DRIVER_IMPLICITY_WAIT_TIME * 1000)

            # 获取用电量数据
            days_xpath = "//div[@id='pane-second' and not(@aria-hidden='true')]//div[contains(@class,'el-table__body-wrapper')]/table/tbody/tr"
            days_element = page.query_selector_all(f"xpath={days_xpath}")
            date = []
            usages = []
            for i in days_element:
                try:
                    day = i.query_selector("td[1]/div").inner_text()
                    usage = i.query_selector("td[2]/div").inner_text()
                    if usage != "":
                        usages.append(usage)
                        date.append(day)
                except Exception:
                    pass
            logging.info(f"DOM 方式成功获取 {len(date)} 天的每日用电量数据")
            return date, usages
        except Exception as e:
            logging.warning(f"DOM 方式获取每日用电量数据失败: {e}")
            return [], []

    def _get_daily_tou_data(self, page: Page):
        """通过展开日用电量表格行获取每日分时电量（谷/平/峰/尖）"""
        tou_rows = []
        try:
            # 找到所有展开图标并逐个点击
            expand_icons = page.query_selector_all(".el-table__expand-icon")
            for icon in expand_icons:
                try:
                    icon.click()
                    time.sleep(0.5)
                except Exception:
                    continue

            time.sleep(1)

            # 读取展开行中的分时电量
            expanded_cells = page.query_selector_all(".el-table__expanded-cell .drop-box-left")
            for cell in expanded_cells:
                tou = {"valley_usage": 0.0, "flat_usage": 0.0, "peak_usage": 0.0, "tip_usage": 0.0}
                paragraphs = cell.query_selector_all("p")
                for p in paragraphs:
                    text = p.inner_text()
                    try:
                        num_el = p.query_selector(".num")
                        val = float(num_el.inner_text())
                    except Exception:
                        continue
                    if "谷" in text:
                        tou["valley_usage"] = val
                    elif "平" in text:
                        tou["flat_usage"] = val
                    elif "峰" in text:
                        tou["peak_usage"] = val
                    elif "尖" in text:
                        tou["tip_usage"] = val
                tou_rows.append(tou)
            logging.info(f"通过展开行获取到 {len(tou_rows)} 条分时电量数据")
        except Exception as e:
            logging.warning(f"获取展开行分时电量失败: {e}")
        return tou_rows

    def _get_bill_detail(self, page: Page, user_id):
        """从用电量页面通过 Vue state 获取月度分时电量"""
        logging.info(f"[{user_id}] 尝试从当前页面获取电费账单分时数据...")
        try:
            # 不再跳转到 403 的 BILL_SUMMARY_URL, 直接从当前页面提取
            components = vue_state.selected_vue_data(page)
            bill = vue_state.normalize_bill_detail(components)
            if bill.get("month"):
                logging.info(f"[{user_id}] 账单分时数据: {bill['month']}, "
                             f"谷={bill.get('valley_usage')}, 平={bill.get('flat_usage')}, "
                             f"峰={bill.get('peak_usage')}, 尖={bill.get('tip_usage')}")
                return bill
            logging.info(f"[{user_id}] Vue state 中未找到账单数据, 跳过")
            return None
        except Exception as e:
            logging.warning(f"[{user_id}] 获取账单分时数据异常: {e}")
            return None

    def _save_user_data(self, user_id, balance, enhanced_balance,
                        last_daily_date, last_daily_usage,
                        date_list, usage_list,
                        month, month_usage, month_charge,
                        yearly_charge, yearly_usage,
                        tou_data=None, bill_tou_data=None, user_name=""):
        if not self.db.connect_user_db(user_id):
            logging.error(f"[{user_id}] 数据库连接失败, 数据未写入")
            return

        try:
            self.db.upsert_user(user_id, self._username, user_name)
            logging.info(f"[{user_id}] 用户信息已更新 (user_name={user_name})")

            # 写入余额日志
            if balance is not None:
                bal_data = {"balance": balance, "user_id": user_id}
                if enhanced_balance:
                    bal_data.update({
                        "as_of": enhanced_balance.get("as_of"),
                        "amount_due": enhanced_balance.get("amount_due"),
                    })
                self.db.insert_balance_log(bal_data)
                logging.info(f"[{user_id}] 余额日志已写入: {balance} 元")

            # 写入每日用电量（DOM 方式）
            if date_list:
                for i in range(len(date_list)):
                    try:
                        self.db.insert_daily_data({
                            "date": date_list[i],
                            "total_usage": float(usage_list[i]),
                            "user_id": user_id,
                        })
                    except Exception as e:
                        logging.debug(f"[{user_id}] 日用电 {date_list[i]} 写入失败 (可能已存在): {e}")
                logging.info(f"[{user_id}] 每日用电量已写入 {len(date_list)} 条")

            # 写入 Vue state 分时日用电量
            if tou_data and tou_data.get("daily"):
                tou_count = 0
                for row in tou_data["daily"]:
                    try:
                        row["user_id"] = user_id
                        self.db.insert_daily_data(row)
                        tou_count += 1
                    except Exception as e:
                        logging.debug(f"[{user_id}] 分时日用电 {row.get('date')} 写入失败: {e}")
                logging.info(f"[{user_id}] Vue state 分时日用电已写入 {tou_count} 条")

            # 写入月度用电量（DOM 方式）
            if month:
                cur_year = str(datetime.now().year)
                for i in range(len(month)):
                    try:
                        # 将 "1月1日-1月31日" 格式转为 "2026-01"
                        m_text = month[i]
                        m_num = re.search(r'(\d+)月', m_text)
                        m_formatted = f"{cur_year}-{int(m_num.group(1)):02d}" if m_num else m_text
                        self.db.insert_monthly_data({
                            "month": m_formatted,
                            "total_usage": float(month_usage[i]) if month_usage[i] else None,
                            "total_charge": float(month_charge[i]) if month_charge[i] else None,
                            "user_id": user_id,
                        })
                    except Exception as e:
                        logging.debug(f"[{user_id}] 月度 {month[i]} 写入失败: {e}")
                logging.info(f"[{user_id}] 月度用电量已写入 {len(month)} 条")

            # 写入 Vue state 分时月用电量
            if tou_data and tou_data.get("months"):
                for m_row in tou_data["months"]:
                    try:
                        m_row["user_id"] = user_id
                        self.db.insert_monthly_data(m_row)
                    except Exception as e:
                        logging.debug(f"[{user_id}] 分时月度 {m_row.get('month')} 写入失败: {e}")
                logging.info(f"[{user_id}] Vue state 分时月用电已写入 {len(tou_data['months'])} 条")

            # 写入账单分时月用电量
            if bill_tou_data and bill_tou_data.get("month"):
                try:
                    self.db.insert_monthly_data({
                        "month": bill_tou_data["month"],
                        "total_usage": bill_tou_data.get("usage"),
                        "total_charge": bill_tou_data.get("charge"),
                        "valley_usage": bill_tou_data.get("valley_usage", 0),
                        "flat_usage": bill_tou_data.get("flat_usage", 0),
                        "peak_usage": bill_tou_data.get("peak_usage", 0),
                        "tip_usage": bill_tou_data.get("tip_usage", 0),
                        "user_id": user_id,
                    })
                    logging.info(f"[{user_id}] 账单分时月度数据已写入: {bill_tou_data['month']}")
                except Exception as e:
                    logging.warning(f"[{user_id}] 账单分时月度写入失败: {e}")

            # 写入年度用电量
            year = str(datetime.now().year)
            if yearly_usage is not None or yearly_charge is not None:
                try:
                    year_data = {"year": year, "user_id": user_id}
                    if yearly_usage is not None:
                        year_data["total_usage"] = float(yearly_usage)
                    if yearly_charge is not None:
                        year_data["total_charge"] = float(yearly_charge)
                    self.db.insert_yearly_data(year_data)
                    logging.info(f"[{user_id}] 年度用电量已写入: {year}")
                except Exception as e:
                    logging.warning(f"[{user_id}] 年度用电量写入失败: {e}")

            # 从 Vue state 获取分时年度汇总
            if tou_data and tou_data.get("year"):
                try:
                    self.db.insert_yearly_data({
                        "year": tou_data["year"],
                        "total_usage": tou_data.get("yearly_usage"),
                        "total_charge": tou_data.get("yearly_charge"),
                        "user_id": user_id,
                    })
                    logging.info(f"[{user_id}] Vue state 年度数据已写入: {tou_data['year']}")
                except Exception as e:
                    logging.warning(f"[{user_id}] Vue state 年度写入失败: {e}")

            # 数据清理
            self.db.cleanup_old_data()
            logging.info(f"[{user_id}] 数据清理完成")

        except Exception as e:
            logging.error(f"[{user_id}] 数据保存过程出错: {e}")
        finally:
            self.db.close_connect()

if __name__ == "__main__":
    with open("bg.jpg", "rb") as f:
        test1 = f.read()
        print(type(test1))
        print(test1)
