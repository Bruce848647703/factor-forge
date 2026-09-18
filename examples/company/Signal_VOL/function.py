import json
import os
import duckdb
import pandas as pd
import connectorx as cx
import urllib.parse

RAWDATA = None


def load_config():
    with open(RAWDATA) as f:
        config = json.load(f)
    return config


def read_sql(sql_query, connect_name="legacy_db"):
    config = load_config()
    db_info = config[connect_name]
    con = f"mssql+pymssql://{db_info['user']}:{db_info['password']}@{db_info['host']}:{db_info['port']}/{db_info['database']}"
    df = cx.read_sql(con, sql_query)
    return df


def read_pg(sql_query, connect_name="citus"):
    config = load_config()
    db_info = config[connect_name]
    con = f"postgresql://{db_info['user']}:{db_info['password']}@{db_info['host']}:{db_info['port']}/{db_info['database']}"
    df = cx.read_sql(con, sql_query)
    return df


def read_ob(sql_query, connect_name="oceanbase"):
    config = load_config()
    db_info = config[connect_name]
    user = urllib.parse.quote(db_info["user"], safe="")
    password = urllib.parse.quote(db_info["password"], safe="")
    con = f"mysql://{user}:{password}@{db_info['host']}:{db_info['port']}/{db_info['database']}"
    df = cx.read_sql(con, sql_query)
    return df


def get_transaction_path(date):
    dir_path = f"/data/cephfs/transaction"
    file_path = f"{dir_path}/{pd.to_datetime(date).strftime('%Y%m%d')}.parquet"
    return file_path


def get_minute_path(date, data_type="one_minute"):
    dir_path = f"/data/cephfs/minute/{data_type}"
    file_path = f"{dir_path}/{pd.to_datetime(date).strftime('%Y%m%d')}.parquet"
    return file_path


def get_order_path(date):
    dir_path = f"/data/cephfs/order"
    file_path = f"{dir_path}/{pd.to_datetime(date).strftime('%Y%m%d')}.parquet"
    return file_path


def get_snapshot_path(date):
    dir_path = f"/data/cephfs/snapshot"
    file_path = f"{dir_path}/{pd.to_datetime(date).strftime('%Y%m%d')}.parquet"
    return file_path


def read_transaction(date, cols):
    file_path = get_transaction_path(date)
    if os.path.exists(file_path):
        sql = f"""SELECT {','.join(cols)} FROM read_parquet('{file_path}')"""
        data = duckdb.query(sql).df()
        return data


def read_minute(date, cols, data_type="one_minute"):
    file_path = get_minute_path(date, data_type)
    print(file_path)
    if os.path.exists(file_path):
        sql = f"""SELECT {','.join(cols)} FROM read_parquet('{file_path}')"""
        data = duckdb.query(sql).df()
        data.dropna(inplace=True)
        return data


def read_daily_security_info(date):
    sql = f"""
        SELECT DISTINCT InnerID, SecCode
        FROM legacy_db.DailySecurityInfo
        WHERE DataDate = '{date}'
    """
    data = read_ob(sql)
    return data


def read_daily_runnercode(date):
    sql = f"""
        SELECT SecCode AS runner_code
        FROM legacy_db.DailySecurityInfo
        WHERE DataDate = '{date}'
    """
    data = read_ob(sql)
    return data


def read_trading_date(date):
    sql = f"""
        SELECT MAX(TradeDate) AS TradeDate
        FROM legacy_db.TradingCalendar
        WHERE SecMarket = 83
        AND IsTradeDay = 1
        AND TradeDate <= '{date}'
    """
    data = read_ob(sql)
    trading_date = data.iloc[0, 0].strftime("%Y-%m-%d")
    return trading_date


def read_tradingdays(date, n):
    sql = f"""
        SELECT DataDate AS TradeDate
        FROM legacy_db.TradeCalendar
        WHERE IsTradeDay = 1
        AND SecMarket = 83
        AND DataDate <= '{date}'
        ORDER BY DataDate DESC
        LIMIT {n}
    """
    dates = read_ob(sql)["TradeDate"]
    return dates


def read_secucode_innercode():
    sql = f"""
        SELECT SecCode, InnerID
        FROM legacy_db.SecurityCodeMap
    """
    data = read_ob(sql)
    return data


def read_fundamental(date, item_code, column_name, quarter_num, publ_date_limit=-180):
    sql = f"""
        SELECT InnerID, EndDate, ReportRank, {column_name}
        FROM fundamental_db.FundamentalItem{item_code}
        WHERE DataDate = '{date}'
        AND AnnouncementDate >= DATE_ADD(EndDate, INTERVAL {publ_date_limit} DAY)
        AND ReportRank <= {quarter_num}
    """
    data = read_ob(sql)
    return data


def read_universe(start_date, end_date, table_name):
    sql = f"""
        SELECT DataDate, InnerID, SecCode
        FROM legacy_db.{table_name}
        WHERE DataDate BETWEEN '{start_date}' AND '{end_date}'
    """
    data = read_ob(sql)
    return data


def read_index_components_weight(start_date, end_date, index_name):
    sql = f"""
        SELECT EndDate, SecInnerID AS InnerID, Weight
        FROM legacy_db.IndexWeight_{index_name}
        WHERE EndDate BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY EndDate
    """
    data = read_ob(sql)
    return data


def read_untradable(start_date, end_date):
    sql = f"""
        SELECT DataDate AS TradeDate, InnerID, SecCode
        FROM legacy_db.NonTradableFlags
        WHERE DataDate BETWEEN '{start_date}' AND '{end_date}'
        AND (IsLimitDownFlag = 1 OR IsLimitUpFlag = 1)
        ORDER BY DataDate
    """
    data = read_ob(sql)
    return data


def save_signal(signal, output_dir, value_file, check_file, min_valid_value):
    output_value_file = f"{output_dir}/{value_file}"
    output_check_file = f"{output_dir}/{check_file}"
    notna_stocks = signal["runner_value"].count()
    assert notna_stocks >= min_valid_value, "Not enough non-NA stocks"
    signal_check = pd.DataFrame(
        {
            "Description": ["{} stocks are calculated".format(notna_stocks)],
            "Result": ["Normal"],
        }
    )
    signal.to_json(output_value_file, orient="records")
    signal_check.to_json(output_check_file, orient="records")