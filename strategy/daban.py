import akshare as ak
import requests
import pandas as pd
from datetime import datetime, timedelta

# 飞书机器人的Webhook URL
feishu_webhook_url = 'https://open.feishu.cn/open-apis/bot/v2/hook/883436f5-6ea1-4220-9e9e-9fb7b2c18c8a'
period_days = 20
close_price_threshold = 5.0
volume_threshold = 1.75
market_capital_low_threshold = 30 * 10**8
market_capital_up_threshold = 160 * 10**8
pct_chg_threshold = 9.8  # 涨停的涨幅阈值
# 获取当前日期
today_date = str(datetime.now().strftime('%Y-%m-%d'))
print(f"今天的日期: {today_date}")

yesterday_date = str((datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'))
print(f"昨日的日期: {yesterday_date}")
# # 计算n天前的日期
# n_days_ago = str((datetime.now() - timedelta(days=period_days)).strftime('%Y-%m-%d'))
# print(f"{period_days}天前的日期: {n_days_ago}")

def send_feishu_message(content):
    """发送消息到飞书"""
    headers = {
        'Content-Type': 'application/json'
    }
    data = {
        "msg_type": "text",
        "content": {
            "text": content
        }
    }
    response = requests.post(feishu_webhook_url, json=data, headers=headers)
    return response.json()


def get_real_time_data():
    """获取实时行情数据"""
    stock_real_time_data = ak.stock_zh_a_spot_em()
    real_time_data = stock_real_time_data
    return real_time_data


def get_last_n_trading_days(date_str, n):
    """计算给定日期前的n个交易日"""
    # 获取所有的交易日历史
    trade_days_df = ak.tool_trade_date_hist_sina()

    # 将输入日期字符串转换成datetime对象
    date_format = '%Y-%m-%d'
    date_format_nodash  = '%Y%m%d'
    today_date = datetime.strptime(date_str, date_format)

    # 确保DataFrame中的trade_date列是datetime类型
    trade_days_df['trade_date'] = pd.to_datetime(trade_days_df['trade_date'])

    # 过滤出小于等于今天的所有交易日，并取最近的n个交易日（不包括今天的交易日）
    trading_days = trade_days_df[trade_days_df['trade_date'] < today_date].tail(n)['trade_date'].tolist()

    # 如果没有足够的交易日，可能需要在这里添加一些异常处理逻辑

    # 返回起始日期（第n个交易日前）和结束日期（昨天）
    return trading_days[0].strftime(date_format_nodash), trading_days[-1].strftime(date_format_nodash)


def filter_stocks(real_time_data):
    """根据条件过滤股票"""
    # 筛选出符合要求的股票
    filtered_stocks = []
    start_date, end_date = get_last_n_trading_days(today_date, period_days)
    print(f"{period_days}天前的日期: {start_date}, 昨天的日期: {end_date}")

    for _, row in real_time_data.iterrows():
        code, name, open, price, volume, market_capital = row['代码'], row['名称'], float(row['今开']), float(row['最新价']), row['成交量'], float(row['总市值'])

        # 排除不符合条件的市场及ST标识的股票
        if ('ST' in name) or (code.startswith('30')) or (code.startswith('688')) or (code.startswith('43')) or (code.startswith('8')) or (code.startswith('9')):
            continue

        # 过滤掉总市值超过120亿或价格不大于5的股票
        if market_capital > market_capital_up_threshold or market_capital <= market_capital_low_threshold or price <= close_price_threshold:
            continue

        # 获取过去n天的历史数据
        history_data = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start_date, end_date=end_date, adjust='qfq')

        if history_data.empty or len(history_data) < period_days:  # 确保有足够的历史数据
            continue

        recent_days_volume = history_data['成交量']
        # print(recent_days_volume)
        recent_days_pct_chg = history_data['涨跌幅']

        # 检查是否20天内有涨停，并且当前成交量大于过去20天每一天的成交量
        if any(pct_chg >= pct_chg_threshold for pct_chg in recent_days_pct_chg):  # 假设涨停为9.8%及以上
            continue

        if open > price:
            continue

        if all(volume > daily_volume * volume_threshold for daily_volume in recent_days_volume):
            filtered_stocks.append((code, name))

    return filtered_stocks


if __name__ == '__main__':
    real_time_data = get_real_time_data()
    filtered_stocks = filter_stocks(real_time_data)
    print(f"筛选出的股票数量: {len(filtered_stocks)}\n")

    if len(filtered_stocks) > 0:
        message = f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n满足条件的股票如下：\n"
        for stock in filtered_stocks:
            message += f"\t {stock[1]}({stock[0]})\n"
        print(message)
        send_feishu_message(message)
    else:
        print("没有找到满足条件的股票")
        # send_feishu_message("没有找到满足条件的股票")
