#!/usr/bin/env python3
"""
单次运行脚本：执行一次国家电网数据抓取然后退出。
适用于 GitHub Actions 等定时任务环境。
"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

if "PYTHON_IN_DOCKER" not in os.environ:
    try:
        import dotenv
        dotenv.load_dotenv(verbose=True)
    except ImportError:
        pass

from scripts.data_fetcher import DataFetcher
from scripts.sensor_updater import SensorUpdater
from scripts.support.error_watcher import ErrorWatcher
from scripts.support.credentials import load_login_credentials
from scripts.support.tou_price import TimeOfUsePriceResolver


LOCAL_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def logger_init(level: str):
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.handlers.clear()
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    fmt = logging.Formatter(
        "%(asctime)s  [%(levelname)s] ---- %(message)s",
        "%Y-%m-%d %H:%M:%S"
    )
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def main():
    try:
        credentials = load_login_credentials()
    except Exception as exc:
        logging.error("Failed to load login credentials: %s", exc)
        sys.exit(1)

    if not credentials:
        logging.error("No login credentials configured.")
        sys.exit(1)

    log_level = os.getenv("LOG_LEVEL", "INFO")
    logger_init(log_level)

    logging.info("============================================")
    logging.info("SGCC Data Fetcher - Single Run Mode")
    logging.info("Current time: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logging.info("============================================")

    LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_DATA_DIR / "pages").mkdir(parents=True, exist_ok=True)

    ErrorWatcher.init(
        root_dir=str(LOCAL_DATA_DIR),
        screenshot_dir=str(LOCAL_DATA_DIR / "pages")
    )

    updater = SensorUpdater()
    fetcher = DataFetcher(
        account=credentials[0].account,
        password=credentials[0].password,
        updater=updater,
        credentials=credentials,
    )

    success = False
    retry_times_limit = int(os.getenv("RETRY_TIMES_LIMIT", "5"))

    try:
        for attempt in range(1, retry_times_limit + 1):
            logging.info("Fetch attempt %s/%s", attempt, retry_times_limit)
            try:
                fetcher.fetch()
                success = True
                logging.info("Data fetch completed successfully!")
                break
            except Exception as exc:
                logging.error("Fetch attempt %s failed: %s", attempt, exc)
                if attempt < retry_times_limit:
                    import time
                    wait_time = int(os.getenv("RETRY_WAIT_TIME_OFFSET_UNIT", "5")) * attempt
                    time.sleep(wait_time)

        if not success:
            logging.error("All %s fetch attempts failed.", retry_times_limit)
            sys.exit(1)
    finally:
        try:
            updater.close()
        except Exception:
            pass
        try:
            if updater.db:
                updater.db.close_connect()
        except Exception:
            pass

    logging.info("Single run completed successfully.")


if __name__ == "__main__":
    main()
