#!/usr/bin/env python3
"""
推送脚本：读取 SQLite 数据库中的国家电网数据，通过 HA REST API 推送到 Home Assistant。
"""
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests


ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "homeassistant.db"


def logger_init():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s] ---- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_ha_config():
    """从环境变量读取 HA 配置"""
    hass_url = os.getenv("HASS_URL", "").rstrip("/")
    hass_token = os.getenv("HASS_TOKEN", "")

    if not hass_url:
        raise ValueError("HASS_URL environment variable is required")
    if not hass_token:
        raise ValueError("HASS_TOKEN environment variable is required")

    return hass_url, hass_token


def ha_api_post(hass_url: str, token: str, entity_id: str, state: Any, attributes: dict):
    """调用 HA REST API 更新实体状态"""
    url = f"{hass_url}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "state": state,
        "attributes": attributes,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        logging.info("Updated %s -> %s %s", entity_id, state, attributes.get("unit_of_measurement", ""))
        return True
    except requests.exceptions.RequestException as exc:
        logging.error("Failed to update %s: %s", entity_id, exc)
        return False


def get_db_connection():
    """获取数据库连接"""
    if not DB_PATH.exists():
        logging.error("Database not found at %s", DB_PATH)
        return None
    return sqlite3.connect(DB_PATH)


def get_user_ids(conn: sqlite3.Connection) -> list[str]:
    """获取所有用户 ID"""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT user_id FROM daily_usage LIMIT 1")
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    finally:
        cursor.close()


def get_latest_daily(conn: sqlite3.Connection, user_id: str) -> Optional[dict]:
    """获取最新日用电数据"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT date, total_usage, total_charge, valley_usage, flat_usage, peak_usage, tip_usage
            FROM daily_usage
            WHERE user_id = ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "date": row[0],
            "usage": round(float(row[1] or 0), 2),
            "charge": round(float(row[2] or 0), 2) if row[2] is not None else None,
            "valley": round(float(row[3] or 0), 2),
            "flat": round(float(row[4] or 0), 2),
            "peak": round(float(row[5] or 0), 2),
            "tip": round(float(row[6] or 0), 2),
        }
    finally:
        cursor.close()


def get_current_month_summary(conn: sqlite3.Connection, user_id: str) -> Optional[dict]:
    """获取本月汇总数据"""
    cursor = conn.cursor()
    month = datetime.now().strftime("%Y-%m")
    try:
        cursor.execute(
            """
            SELECT total_usage, total_charge, valley_usage, flat_usage, peak_usage, tip_usage
            FROM monthly_usage
            WHERE user_id = ? AND month = ?
            """,
            (user_id, month),
        )
        row = cursor.fetchone()
        if not row:
            # 尝试从每日数据汇总
            cursor.execute(
                """
                SELECT COALESCE(SUM(total_usage), 0), COALESCE(SUM(total_charge), 0),
                       COALESCE(SUM(valley_usage), 0), COALESCE(SUM(flat_usage), 0),
                       COALESCE(SUM(peak_usage), 0), COALESCE(SUM(tip_usage), 0)
                FROM daily_usage
                WHERE user_id = ? AND substr(date, 1, 7) = ?
                """,
                (user_id, month),
            )
            row = cursor.fetchone()
            if not row or row[0] == 0:
                return None
        return {
            "usage": round(float(row[0] or 0), 2),
            "charge": round(float(row[1] or 0), 2) if row[1] is not None else None,
            "valley": round(float(row[2] or 0), 2),
            "flat": round(float(row[3] or 0), 2),
            "peak": round(float(row[4] or 0), 2),
            "tip": round(float(row[5] or 0), 2),
        }
    finally:
        cursor.close()


def get_current_year_summary(conn: sqlite3.Connection, user_id: str) -> Optional[dict]:
    """获取本年汇总数据"""
    cursor = conn.cursor()
    year = datetime.now().strftime("%Y")
    try:
        cursor.execute(
            """
            SELECT total_usage, total_charge, valley_usage, flat_usage, peak_usage, tip_usage
            FROM yearly_usage
            WHERE user_id = ? AND year = ?
            """,
            (user_id, year),
        )
        row = cursor.fetchone()
        if not row:
            # 尝试从月数据汇总
            cursor.execute(
                """
                SELECT COALESCE(SUM(total_usage), 0), COALESCE(SUM(total_charge), 0)
                FROM monthly_usage
                WHERE user_id = ? AND substr(month, 1, 4) = ?
                """,
                (user_id, year),
            )
            row = cursor.fetchone()
            if not row or row[0] == 0:
                return None
            return {
                "usage": round(float(row[0] or 0), 2),
                "charge": round(float(row[1] or 0), 2) if row[1] is not None else None,
            }
        return {
            "usage": round(float(row[0] or 0), 2),
            "charge": round(float(row[1] or 0), 2) if row[1] is not None else None,
            "valley": round(float(row[2] or 0), 2),
            "flat": round(float(row[3] or 0), 2),
            "peak": round(float(row[4] or 0), 2),
            "tip": round(float(row[5] or 0), 2),
        }
    finally:
        cursor.close()


def get_balance_from_cache(user_id: str) -> Optional[float]:
    """从缓存文件读取余额（因为余额可能不在数据库中）"""
    cache_file = ROOT_DIR / "data" / "ha_95598_cache.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        entry = data.get(user_id, {})
        user_data = entry.get("data", {})
        balance = user_data.get("balance")
        if balance is not None:
            return round(float(balance), 2)
    except Exception as exc:
        logging.warning("Failed to read balance from cache: %s", exc)
    return None


def push_daily_history(conn: sqlite3.Connection, hass_url: str, token: str, user_id: str, user_suffix: str):
    """推送最近 7 天用电历史到 HA"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT date, total_usage, COALESCE(total_charge, 0)
            FROM daily_usage
            WHERE user_id = ?
            ORDER BY date DESC
            LIMIT 7
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return

        rows.reverse()
        series = []
        for row in rows:
            series.append({
                "date": row[0],
                "usage": round(float(row[1] or 0), 2),
                "charge": round(float(row[2] or 0), 2),
            })

        latest = series[-1]
        entity_id = f"sensor.sgcc_daily_history_{user_suffix}"
        ha_api_post(
            hass_url, token, entity_id,
            state=latest["usage"],
            attributes={
                "friendly_name": f"国网近7日用电_{user_suffix}",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "latest_date": latest["date"],
                "series": series,
                "state_class": "measurement",
            },
        )
    finally:
        cursor.close()


def main():
    logger_init()
    logging.info("============================================")
    logging.info("Push SGCC Data to Home Assistant")
    logging.info("============================================")

    # 读取配置
    try:
        hass_url, hass_token = get_ha_config()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    logging.info("HA URL: %s", hass_url)

    # 连接数据库
    conn = get_db_connection()
    if not conn:
        sys.exit(1)

    try:
        user_ids = get_user_ids(conn)
        if not user_ids:
            logging.error("No user data found in database.")
            sys.exit(1)

        for user_id in user_ids:
            user_suffix = user_id[-4:]  # 使用后 4 位作为后缀
            logging.info("Processing user: %s", user_suffix)

            # 推送余额
            balance = get_balance_from_cache(user_id)
            if balance is not None:
                ha_api_post(
                    hass_url, hass_token,
                    f"sensor.sgcc_balance_{user_suffix}",
                    state=balance,
                    attributes={
                        "friendly_name": f"国网电费余额_{user_suffix}",
                        "unit_of_measurement": "CNY",
                        "device_class": "monetary",
                        "state_class": "measurement",
                    },
                )

            # 推送昨日用电
            daily = get_latest_daily(conn, user_id)
            if daily:
                ha_api_post(
                    hass_url, hass_token,
                    f"sensor.sgcc_daily_usage_{user_suffix}",
                    state=daily["usage"],
                    attributes={
                        "friendly_name": f"国网昨日用电_{user_suffix}",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                        "date": daily["date"],
                        "charge": daily["charge"],
                    },
                )
                if daily["charge"] is not None:
                    ha_api_post(
                        hass_url, hass_token,
                        f"sensor.sgcc_daily_charge_{user_suffix}",
                        state=daily["charge"],
                        attributes={
                            "friendly_name": f"国网昨日电费_{user_suffix}",
                            "unit_of_measurement": "CNY",
                            "device_class": "monetary",
                            "state_class": "measurement",
                            "date": daily["date"],
                        },
                    )

            # 推送本月用电
            month = get_current_month_summary(conn, user_id)
            if month:
                ha_api_post(
                    hass_url, hass_token,
                    f"sensor.sgcc_month_usage_{user_suffix}",
                    state=month["usage"],
                    attributes={
                        "friendly_name": f"国网本月用电_{user_suffix}",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
                if month["charge"] is not None:
                    ha_api_post(
                        hass_url, hass_token,
                        f"sensor.sgcc_month_charge_{user_suffix}",
                        state=month["charge"],
                        attributes={
                            "friendly_name": f"国网本月电费_{user_suffix}",
                            "unit_of_measurement": "CNY",
                            "device_class": "monetary",
                            "state_class": "measurement",
                        },
                    )

            # 推送本年用电
            year = get_current_year_summary(conn, user_id)
            if year:
                ha_api_post(
                    hass_url, hass_token,
                    f"sensor.sgcc_year_usage_{user_suffix}",
                    state=year["usage"],
                    attributes={
                        "friendly_name": f"国网本年用电_{user_suffix}",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
                if year["charge"] is not None:
                    ha_api_post(
                        hass_url, hass_token,
                        f"sensor.sgcc_year_charge_{user_suffix}",
                        state=year["charge"],
                        attributes={
                            "friendly_name": f"国网本年电费_{user_suffix}",
                            "unit_of_measurement": "CNY",
                            "device_class": "monetary",
                            "state_class": "measurement",
                        },
                    )

            # 推送历史数据
            push_daily_history(conn, hass_url, hass_token, user_id, user_suffix)

        logging.info("============================================")
        logging.info("Push completed successfully!")
        logging.info("============================================")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
