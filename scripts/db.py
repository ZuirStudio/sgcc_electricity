import logging
import os
import re

import sqlite3
import mysql.connector
from datetime import datetime, timedelta

class DB:
    def connect_user_db(self, user_id):
        # 连接用户数据库的逻辑
        pass

    def insert_data(self, data: dict):
        # 向数据库插入数据的逻辑
        pass

    def insert_expand_data(self, data: dict):
        # 向数据库插入扩展数据的逻辑
        pass

    def close_connect(self):
        # 关闭连接
        pass

    def upsert_user(self, user_id, user, user_name):
        # 更新用户信息
        pass

    def cleanup_old_data(self):
        # 清理过旧数据
        pass

    def insert_daily_data(self, data: dict):
        # 插入每日数据
        pass

    def insert_monthly_data(self, data: dict):
        # 插入每月数据
        pass

    def insert_balance_log(self, data: dict):
        # 插入余额日志
        pass

    def insert_yearly_data(self, data: dict):
        # 插入每年数据
        pass


class SqliteDB(DB):
    def __init__(self):
        self.connect = None
        self.user_id = None

    def connect_user_db(self, user_id):
        """创建数据库集合，db_name = homeassistant.db
        :param user_id: 用户ID"""
        try:
            self.user_id = user_id
            DB_NAME = os.getenv("DB_NAME", "homeassistant.db")
            if 'PYTHON_IN_DOCKER' in os.environ:
                DB_NAME = "/data/" + DB_NAME
            self.connect = sqlite3.connect(DB_NAME)
            self.connect.execute("PRAGMA foreign_keys = ON")
            logging.info(f"数据库 {DB_NAME} 连接成功。")
            
            self._create_tables()
            return True
        except sqlite3.Error as e:
            logging.error(f"创建数据库或数据表错误: {e}")
            return False

    def _create_tables(self):
        """创建所有需要的表"""
        cursor = self.connect.cursor()
        try:
            # 用户信息表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_users (
                    user_id TEXT PRIMARY KEY NOT NULL,
                    username TEXT,
                    user_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 余额日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_balance_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    balance REAL NOT NULL,
                    amount_due REAL,
                    as_of TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_balance_user ON sg_balance_log(user_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_balance_time ON sg_balance_log(created_at)
            ''')

            # 日用电量表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    total_usage REAL,
                    valley_usage REAL DEFAULT 0,
                    flat_usage REAL DEFAULT 0,
                    peak_usage REAL DEFAULT 0,
                    tip_usage REAL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id),
                    UNIQUE(user_id, date)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_daily_user ON sg_daily(user_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_daily_date ON sg_daily(date)
            ''')

            # 月用电量表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_monthly (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    total_usage REAL,
                    total_charge REAL,
                    valley_usage REAL DEFAULT 0,
                    flat_usage REAL DEFAULT 0,
                    peak_usage REAL DEFAULT 0,
                    tip_usage REAL DEFAULT 0,
                    begin_date TEXT,
                    end_date TEXT,
                    meter_read_time TEXT,
                    is_max INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id),
                    UNIQUE(user_id, month)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_monthly_user ON sg_monthly(user_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_monthly_month ON sg_monthly(month)
            ''')

            # 年用电量表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_yearly (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    year TEXT NOT NULL,
                    total_usage REAL,
                    total_charge REAL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id),
                    UNIQUE(user_id, year)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_yearly_user ON sg_yearly(user_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_yearly_year ON sg_yearly(year)
            ''')

            self.connect.commit()
            logging.info(f"数据表创建成功")
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"创建数据表错误: {e}")
        finally:
            cursor.close()

    def upsert_user(self, user_id, username, user_name):
        """更新或插入用户信息"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return

        logging.info(f"upsert_user: user_id: {user_id}, user: {username}, user_name: {user_name}")
        try:
            cursor = self.connect.cursor()
            
            cursor.execute('''
                SELECT user_id FROM sg_users WHERE user_id = ?
            ''', (user_id,))
            exists = cursor.fetchone()

            if exists:
                cursor.execute('''
                    UPDATE sg_users 
                    SET username = ?, user_name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                ''', (username, user_name, user_id))
            else:
                cursor.execute('''
                    INSERT INTO sg_users (user_id, username, user_name)
                    VALUES (?, ?, ?)
                ''', (user_id, username, user_name))

            self.connect.commit()
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"upsert_user 失败: {e}")
        finally:
            cursor.close()

    def insert_balance_log(self, data: dict):
        """插入余额日志"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_balance_log: {data}")
        try:
            cursor = self.connect.cursor()
            cursor.execute('''
                INSERT INTO sg_balance_log (user_id, balance, amount_due, as_of)
                VALUES (?, ?, ?, ?)
            ''', (
                data.get("user_id"),
                data.get("balance"),
                data.get("amount_due"),
                data.get("as_of")
            ))
            self.connect.commit()
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"insert_balance_log 失败: {e}")
        finally:
            cursor.close()

    def insert_daily_data(self, data: dict):
        """插入或更新每日数据（支持合并 DOM 和 Vue state 数据）"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_daily_data: {data}")
        try:
            cursor = self.connect.cursor()
            user_id = data.get("user_id")
            date_str = data.get("date")
            
            cursor.execute('''
                SELECT id, total_usage, valley_usage, flat_usage, peak_usage, tip_usage 
                FROM sg_daily WHERE user_id = ? AND date = ?
            ''', (user_id, date_str))
            existing = cursor.fetchone()

            if existing:
                updates = []
                params = []
                record_id = existing[0]
                
                if data.get("total_usage") is not None and existing[1] is None:
                    updates.append("total_usage = ?")
                    params.append(data.get("total_usage"))
                elif data.get("total_usage") is not None:
                    updates.append("total_usage = ?")
                    params.append(data.get("total_usage"))

                if data.get("valley_usage") is not None and existing[2] == 0:
                    updates.append("valley_usage = ?")
                    params.append(data.get("valley_usage"))
                elif data.get("valley_usage") is not None:
                    updates.append("valley_usage = ?")
                    params.append(data.get("valley_usage"))

                if data.get("flat_usage") is not None and existing[3] == 0:
                    updates.append("flat_usage = ?")
                    params.append(data.get("flat_usage"))
                elif data.get("flat_usage") is not None:
                    updates.append("flat_usage = ?")
                    params.append(data.get("flat_usage"))

                if data.get("peak_usage") is not None and existing[4] == 0:
                    updates.append("peak_usage = ?")
                    params.append(data.get("peak_usage"))
                elif data.get("peak_usage") is not None:
                    updates.append("peak_usage = ?")
                    params.append(data.get("peak_usage"))

                if data.get("tip_usage") is not None and existing[5] == 0:
                    updates.append("tip_usage = ?")
                    params.append(data.get("tip_usage"))
                elif data.get("tip_usage") is not None:
                    updates.append("tip_usage = ?")
                    params.append(data.get("tip_usage"))

                if updates:
                    updates.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(record_id)
                    sql = f"UPDATE sg_daily SET {', '.join(updates)} WHERE id = ?"
                    cursor.execute(sql, params)
            else:
                cursor.execute('''
                    INSERT INTO sg_daily 
                    (user_id, date, total_usage, valley_usage, flat_usage, peak_usage, tip_usage)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id,
                    date_str,
                    data.get("total_usage"),
                    data.get("valley_usage", 0),
                    data.get("flat_usage", 0),
                    data.get("peak_usage", 0),
                    data.get("tip_usage", 0)
                ))

            self.connect.commit()
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"insert_daily_data 失败: {e}")
        finally:
            cursor.close()

    def insert_monthly_data(self, data: dict):
        """插入或更新月度数据（支持合并 DOM 和 Vue state 数据）"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_monthly_data: {data}")
        try:
            cursor = self.connect.cursor()
            user_id = data.get("user_id")
            month_str = data.get("month")
            
            cursor.execute('''
                SELECT id, total_usage, total_charge, valley_usage, flat_usage, peak_usage, tip_usage,
                       begin_date, end_date, meter_read_time, is_max
                FROM sg_monthly WHERE user_id = ? AND month = ?
            ''', (user_id, month_str))
            existing = cursor.fetchone()

            if existing:
                updates = []
                params = []
                record_id = existing[0]
                
                if data.get("total_usage") is not None and existing[1] is None:
                    updates.append("total_usage = ?")
                    params.append(data.get("total_usage"))
                elif data.get("total_usage") is not None:
                    updates.append("total_usage = ?")
                    params.append(data.get("total_usage"))

                if data.get("total_charge") is not None and existing[2] is None:
                    updates.append("total_charge = ?")
                    params.append(data.get("total_charge"))
                elif data.get("total_charge") is not None:
                    updates.append("total_charge = ?")
                    params.append(data.get("total_charge"))

                if data.get("valley_usage") is not None and (existing[3] is None or existing[3] == 0):
                    updates.append("valley_usage = ?")
                    params.append(data.get("valley_usage"))
                elif data.get("valley_usage") is not None:
                    updates.append("valley_usage = ?")
                    params.append(data.get("valley_usage"))

                if data.get("flat_usage") is not None and (existing[4] is None or existing[4] == 0):
                    updates.append("flat_usage = ?")
                    params.append(data.get("flat_usage"))
                elif data.get("flat_usage") is not None:
                    updates.append("flat_usage = ?")
                    params.append(data.get("flat_usage"))

                if data.get("peak_usage") is not None and (existing[5] is None or existing[5] == 0):
                    updates.append("peak_usage = ?")
                    params.append(data.get("peak_usage"))
                elif data.get("peak_usage") is not None:
                    updates.append("peak_usage = ?")
                    params.append(data.get("peak_usage"))

                if data.get("tip_usage") is not None and (existing[6] is None or existing[6] == 0):
                    updates.append("tip_usage = ?")
                    params.append(data.get("tip_usage"))
                elif data.get("tip_usage") is not None:
                    updates.append("tip_usage = ?")
                    params.append(data.get("tip_usage"))

                if data.get("begin_date") is not None and existing[7] is None:
                    updates.append("begin_date = ?")
                    params.append(data.get("begin_date"))
                if data.get("end_date") is not None and existing[8] is None:
                    updates.append("end_date = ?")
                    params.append(data.get("end_date"))
                if data.get("meter_read_time") is not None and existing[9] is None:
                    updates.append("meter_read_time = ?")
                    params.append(data.get("meter_read_time"))
                if data.get("is_max") is not None:
                    updates.append("is_max = ?")
                    params.append(1 if data.get("is_max") else 0)

                if updates:
                    updates.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(record_id)
                    sql = f"UPDATE sg_monthly SET {', '.join(updates)} WHERE id = ?"
                    cursor.execute(sql, params)
            else:
                cursor.execute('''
                    INSERT INTO sg_monthly 
                    (user_id, month, total_usage, total_charge, valley_usage, flat_usage, peak_usage, tip_usage,
                     begin_date, end_date, meter_read_time, is_max)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id,
                    month_str,
                    data.get("total_usage"),
                    data.get("total_charge"),
                    data.get("valley_usage", 0),
                    data.get("flat_usage", 0),
                    data.get("peak_usage", 0),
                    data.get("tip_usage", 0),
                    data.get("begin_date"),
                    data.get("end_date"),
                    data.get("meter_read_time"),
                    1 if data.get("is_max") else 0
                ))

            self.connect.commit()
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"insert_monthly_data 失败: {e}")
        finally:
            cursor.close()

    def insert_yearly_data(self, data: dict):
        """插入或更新年度数据（支持合并 DOM 和 Vue state 数据）"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_yearly_data: {data}")
        try:
            cursor = self.connect.cursor()
            user_id = data.get("user_id")
            year_str = data.get("year")
            
            cursor.execute('''
                SELECT id, total_usage, total_charge
                FROM sg_yearly WHERE user_id = ? AND year = ?
            ''', (user_id, year_str))
            existing = cursor.fetchone()

            if existing:
                updates = []
                params = []
                record_id = existing[0]
                
                if data.get("total_usage") is not None and existing[1] is None:
                    updates.append("total_usage = ?")
                    params.append(data.get("total_usage"))
                elif data.get("total_usage") is not None:
                    updates.append("total_usage = ?")
                    params.append(data.get("total_usage"))

                if data.get("total_charge") is not None and existing[2] is None:
                    updates.append("total_charge = ?")
                    params.append(data.get("total_charge"))
                elif data.get("total_charge") is not None:
                    updates.append("total_charge = ?")
                    params.append(data.get("total_charge"))

                if updates:
                    updates.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(record_id)
                    sql = f"UPDATE sg_yearly SET {', '.join(updates)} WHERE id = ?"
                    cursor.execute(sql, params)
            else:
                cursor.execute('''
                    INSERT INTO sg_yearly 
                    (user_id, year, total_usage, total_charge)
                    VALUES (?, ?, ?, ?)
                ''', (
                    user_id,
                    year_str,
                    data.get("total_usage"),
                    data.get("total_charge")
                ))

            self.connect.commit()
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"insert_yearly_data 失败: {e}")
        finally:
            cursor.close()

    def cleanup_old_data(self):
        """清理过旧数据（保留最近2年的数据）"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return

        logging.info("cleanup_old_data")
        try:
            cursor = self.connect.cursor()
            user_id = self.user_id
            cutoff_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
            
            cursor.execute('''
                DELETE FROM sg_daily WHERE user_id = ? AND date < ?
            ''', (user_id, cutoff_date))
            
            cutoff_year = str(datetime.now().year - 2)
            cursor.execute('''
                DELETE FROM sg_monthly WHERE user_id = ? AND month < ?
            ''', (user_id, f"{cutoff_year}-01"))
            
            cursor.execute('''
                DELETE FROM sg_yearly WHERE user_id = ? AND year < ?
            ''', (user_id, cutoff_year))

            self.connect.commit()
        except sqlite3.Error as e:
            self.connect.rollback()
            logging.error(f"cleanup_old_data 失败: {e}")
        finally:
            cursor.close()

    def insert_data(self, data: dict):
        """兼容旧接口的方法"""
        if self.connect is None:
            logging.error("数据库连接未建立。")
            return
        try:
            self.insert_daily_data({
                "user_id": self.user_id,
                "date": data.get("date"),
                "total_usage": data.get("usage")
            })
        except BaseException as e:
            logging.debug(f"数据更新失败: {e}")

    def insert_expand_data(self, data: dict):
        """兼容旧接口的方法"""
        pass

    def close_connect(self):
        if self.connect:
            self.connect.close()
            self.connect = None
            logging.info("数据库连接已关闭。")


class MysqlDB(DB):
    def __init__(self):
        self.connect = None
        self.user_id = None

    def connect_user_db(self, user_id):
        try:
            self.user_id = user_id
            host = os.getenv("MYSQL_HOST")
            user = os.getenv("MYSQL_USER")
            password = os.getenv("MYSQL_PASSWORD")
            database = os.getenv("MYSQL_DATABASE")
            port = int(os.getenv("MYSQL_PORT", 3306))
            self.connect = mysql.connector.connect(
                host=host,
                user=user,
                password=password,
                database=database,
                port=port
            )

            if self.connect.is_connected():
                logging.info(f"已连接 MySQL 数据库。")
                self._create_tables()
                return True
            else:
                logging.error("连接 MySQL 数据库失败。")
                return False
        except BaseException as e:
            logging.error(f"缺少 MySQL 配置: {e}")
            return False

    def _create_tables(self):
        """创建所有需要的表"""
        try:
            cursor = self.connect.cursor()
            
            # 用户信息表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_users (
                    user_id VARCHAR(50) PRIMARY KEY COMMENT '用户ID',
                    username VARCHAR(100) COMMENT '登录用户名',
                    user_name VARCHAR(100) COMMENT '用户名称',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户信息表'
            ''')

            # 余额日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_balance_log (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                    user_id VARCHAR(50) NOT NULL COMMENT '用户ID',
                    balance DECIMAL(10,2) NOT NULL COMMENT '账户余额',
                    amount_due DECIMAL(10,2) COMMENT '应交金额',
                    as_of VARCHAR(50) COMMENT '数据时间',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    INDEX idx_balance_user (user_id),
                    INDEX idx_balance_time (created_at),
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='余额日志表'
            ''')

            # 日用电量表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_daily (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                    user_id VARCHAR(50) NOT NULL COMMENT '用户ID',
                    date VARCHAR(20) NOT NULL COMMENT '日期',
                    total_usage DECIMAL(10,2) COMMENT '总用电量(度)',
                    valley_usage DECIMAL(10,2) DEFAULT 0 COMMENT '谷段用电量(度)',
                    flat_usage DECIMAL(10,2) DEFAULT 0 COMMENT '平段用电量(度)',
                    peak_usage DECIMAL(10,2) DEFAULT 0 COMMENT '峰段用电量(度)',
                    tip_usage DECIMAL(10,2) DEFAULT 0 COMMENT '尖段用电量(度)',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_daily_user (user_id),
                    INDEX idx_daily_date (date),
                    UNIQUE KEY uk_user_date (user_id, date),
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日用电量表'
            ''')

            # 月用电量表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_monthly (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                    user_id VARCHAR(50) NOT NULL COMMENT '用户ID',
                    month VARCHAR(20) NOT NULL COMMENT '月份',
                    total_usage DECIMAL(10,2) COMMENT '总用电量(度)',
                    total_charge DECIMAL(10,2) COMMENT '总电费(元)',
                    valley_usage DECIMAL(10,2) DEFAULT 0 COMMENT '谷段用电量(度)',
                    flat_usage DECIMAL(10,2) DEFAULT 0 COMMENT '平段用电量(度)',
                    peak_usage DECIMAL(10,2) DEFAULT 0 COMMENT '峰段用电量(度)',
                    tip_usage DECIMAL(10,2) DEFAULT 0 COMMENT '尖段用电量(度)',
                    begin_date VARCHAR(50) COMMENT '开始日期',
                    end_date VARCHAR(50) COMMENT '结束日期',
                    meter_read_time VARCHAR(50) COMMENT '抄表时间',
                    is_max TINYINT DEFAULT 0 COMMENT '是否最大值',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_monthly_user (user_id),
                    INDEX idx_monthly_month (month),
                    UNIQUE KEY uk_user_month (user_id, month),
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='月用电量表'
            ''')

            # 年用电量表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sg_yearly (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                    user_id VARCHAR(50) NOT NULL COMMENT '用户ID',
                    year VARCHAR(10) NOT NULL COMMENT '年份',
                    total_usage DECIMAL(10,2) COMMENT '总用电量(度)',
                    total_charge DECIMAL(10,2) COMMENT '总电费(元)',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_yearly_user (user_id),
                    INDEX idx_yearly_year (year),
                    UNIQUE KEY uk_user_year (user_id, year),
                    FOREIGN KEY (user_id) REFERENCES sg_users(user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='年用电量表'
            ''')

            self.connect.commit()
            logging.info(f"数据表创建成功")
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"创建数据表错误: {e}")
        finally:
            if cursor:
                cursor.close()

    def upsert_user(self, user_id, username, user_name):
        """更新或插入用户信息"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return

        logging.info(f"upsert_user: user_id: {user_id}, user: {username}, user_name: {user_name}")
        try:
            cursor = self.connect.cursor()
            
            cursor.execute('''
                INSERT INTO sg_users (user_id, username, user_name)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    username = VALUES(username),
                    user_name = VALUES(user_name),
                    updated_at = CURRENT_TIMESTAMP
            ''', (user_id, username, user_name))

            self.connect.commit()
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"upsert_user 失败: {e}")
        finally:
            if cursor:
                cursor.close()

    def insert_balance_log(self, data: dict):
        """插入余额日志"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_balance_log: {data}")
        try:
            cursor = self.connect.cursor()
            cursor.execute('''
                INSERT INTO sg_balance_log (user_id, balance, amount_due, as_of)
                VALUES (%s, %s, %s, %s)
            ''', (
                data.get("user_id"),
                data.get("balance"),
                data.get("amount_due"),
                data.get("as_of")
            ))
            self.connect.commit()
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"insert_balance_log 失败: {e}")
        finally:
            if cursor:
                cursor.close()

    def insert_daily_data(self, data: dict):
        """插入或更新每日数据（支持合并 DOM 和 Vue state 数据）"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_daily_data: {data}")
        try:
            cursor = self.connect.cursor()
            
            cursor.execute('''
                INSERT INTO sg_daily 
                (user_id, date, total_usage, valley_usage, flat_usage, peak_usage, tip_usage)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_usage = COALESCE(VALUES(total_usage), total_usage),
                    valley_usage = COALESCE(NULLIF(VALUES(valley_usage), 0), valley_usage),
                    flat_usage = COALESCE(NULLIF(VALUES(flat_usage), 0), flat_usage),
                    peak_usage = COALESCE(NULLIF(VALUES(peak_usage), 0), peak_usage),
                    tip_usage = COALESCE(NULLIF(VALUES(tip_usage), 0), tip_usage),
                    updated_at = CURRENT_TIMESTAMP
            ''', (
                data.get("user_id"),
                data.get("date"),
                data.get("total_usage"),
                data.get("valley_usage", 0),
                data.get("flat_usage", 0),
                data.get("peak_usage", 0),
                data.get("tip_usage", 0)
            ))

            self.connect.commit()
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"insert_daily_data 失败: {e}")
        finally:
            if cursor:
                cursor.close()

    def insert_monthly_data(self, data: dict):
        """插入或更新月度数据（支持合并 DOM 和 Vue state 数据）"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_monthly_data: {data}")
        try:
            cursor = self.connect.cursor()
            
            cursor.execute('''
                INSERT INTO sg_monthly 
                (user_id, month, total_usage, total_charge, valley_usage, flat_usage, peak_usage, tip_usage,
                 begin_date, end_date, meter_read_time, is_max)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_usage = COALESCE(VALUES(total_usage), total_usage),
                    total_charge = COALESCE(VALUES(total_charge), total_charge),
                    valley_usage = COALESCE(NULLIF(VALUES(valley_usage), 0), valley_usage),
                    flat_usage = COALESCE(NULLIF(VALUES(flat_usage), 0), flat_usage),
                    peak_usage = COALESCE(NULLIF(VALUES(peak_usage), 0), peak_usage),
                    tip_usage = COALESCE(NULLIF(VALUES(tip_usage), 0), tip_usage),
                    begin_date = COALESCE(VALUES(begin_date), begin_date),
                    end_date = COALESCE(VALUES(end_date), end_date),
                    meter_read_time = COALESCE(VALUES(meter_read_time), meter_read_time),
                    is_max = COALESCE(VALUES(is_max), is_max),
                    updated_at = CURRENT_TIMESTAMP
            ''', (
                data.get("user_id"),
                data.get("month"),
                data.get("total_usage"),
                data.get("total_charge"),
                data.get("valley_usage", 0),
                data.get("flat_usage", 0),
                data.get("peak_usage", 0),
                data.get("tip_usage", 0),
                data.get("begin_date"),
                data.get("end_date"),
                data.get("meter_read_time"),
                1 if data.get("is_max") else 0
            ))

            self.connect.commit()
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"insert_monthly_data 失败: {e}")
        finally:
            if cursor:
                cursor.close()

    def insert_yearly_data(self, data: dict):
        """插入或更新年度数据（支持合并 DOM 和 Vue state 数据）"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return

        logging.info(f"insert_yearly_data: {data}")
        try:
            cursor = self.connect.cursor()
            
            cursor.execute('''
                INSERT INTO sg_yearly 
                (user_id, year, total_usage, total_charge)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_usage = COALESCE(VALUES(total_usage), total_usage),
                    total_charge = COALESCE(VALUES(total_charge), total_charge),
                    updated_at = CURRENT_TIMESTAMP
            ''', (
                data.get("user_id"),
                data.get("year"),
                data.get("total_usage"),
                data.get("total_charge")
            ))

            self.connect.commit()
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"insert_yearly_data 失败: {e}")
        finally:
            if cursor:
                cursor.close()

    def cleanup_old_data(self):
        """清理过旧数据（保留最近2年的数据）"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return

        logging.info("cleanup_old_data")
        try:
            cursor = self.connect.cursor()
            user_id = self.user_id
            cutoff_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
            
            cursor.execute('''
                DELETE FROM sg_daily WHERE user_id = %s AND date < %s
            ''', (user_id, cutoff_date))
            
            cutoff_year = str(datetime.now().year - 2)
            cursor.execute('''
                DELETE FROM sg_monthly WHERE user_id = %s AND month < %s
            ''', (user_id, f"{cutoff_year}-01"))
            
            cursor.execute('''
                DELETE FROM sg_yearly WHERE user_id = %s AND year < %s
            ''', (user_id, cutoff_year))

            self.connect.commit()
        except mysql.connector.Error as e:
            self.connect.rollback()
            logging.error(f"cleanup_old_data 失败: {e}")
        finally:
            if cursor:
                cursor.close()

    def insert_data(self, data: dict):
        """兼容旧接口的方法"""
        if self.connect is None or not self.connect.is_connected():
            logging.error("数据库连接未建立。")
            return
        try:
            self.insert_daily_data({
                "user_id": self.user_id,
                "date": data.get("date"),
                "total_usage": data.get("usage")
            })
        except BaseException as e:
            logging.error(f"数据更新失败: {e}")

    def insert_expand_data(self, data: dict):
        """兼容旧接口的方法"""
        pass

    def close_connect(self):
        if self.connect and self.connect.is_connected():
            self.connect.close()
            self.connect = None
            logging.info("MySQL 数据库连接已关闭。")
