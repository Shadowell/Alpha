"""
股票分析飞书机器人

功能：
1. 筛选符合条件的股票
2. 分析股票所属概念板块
3. 生成热门概念汇总
4. 发送文字和图片消息到飞书群

飞书图片发送配置说明：
要启用图片发送功能，需要完成以下步骤：

1. 创建飞书应用：
   - 访问 https://open.feishu.cn/
   - 登录并创建企业自建应用
   - 记录 app_id 和 app_secret

2. 开通权限：
   - 在应用管理页面，进入"权限管理"
   - 搜索并开通"获取与上传图片或文件资源"权限
   - 发布应用版本

3. 配置代码：
   - 将下面的 FEISHU_APP_ID 和 FEISHU_APP_SECRET 替换为实际值
   - 如果不需要图片功能，可以保持默认值，程序会自动跳过图片发送

注意：如果没有配置正确的app_id和app_secret，程序仍会正常运行，只是不会发送图片到飞书。
"""

import akshare as ak
import requests
import pandas as pd
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
import textwrap
import base64
import io
import os

# ANSI颜色代码定义
class Colors:
    # 基础颜色
    RED = '\033[91m'      # 红色 - 用于下跌、错误
    GREEN = '\033[92m'    # 绿色 - 用于上涨、成功
    YELLOW = '\033[93m'   # 黄色 - 用于警告、平盘
    BLUE = '\033[94m'     # 蓝色 - 用于信息
    PURPLE = '\033[95m'   # 紫色 - 用于特殊标记
    CYAN = '\033[96m'     # 青色 - 用于标题
    WHITE = '\033[97m'    # 白色
    
    # 背景颜色
    BG_RED = '\033[101m'
    BG_GREEN = '\033[102m'
    BG_YELLOW = '\033[103m'
    
    # 样式
    BOLD = '\033[1m'      # 粗体
    UNDERLINE = '\033[4m' # 下划线
    
    # 重置
    RESET = '\033[0m'     # 重置所有格式
    
    @staticmethod
    def colorize(text, color):
        """给文本添加颜色"""
        return f"{color}{text}{Colors.RESET}"
    
    @staticmethod
    def red(text):
        return Colors.colorize(text, Colors.RED)
    
    @staticmethod
    def green(text):
        return Colors.colorize(text, Colors.GREEN)
    
    @staticmethod
    def yellow(text):
        return Colors.colorize(text, Colors.YELLOW)
    
    @staticmethod
    def blue(text):
        return Colors.colorize(text, Colors.BLUE)
    
    @staticmethod
    def purple(text):
        return Colors.colorize(text, Colors.PURPLE)
    
    @staticmethod
    def cyan(text):
        return Colors.colorize(text, Colors.CYAN)
    
    @staticmethod
    def bold(text):
        return Colors.colorize(text, Colors.BOLD)

# 需要过滤的概念板块关键词
FILTERED_CONCEPT_KEYWORDS = [
    '昨日', '今日', '昨天', '今天',
    '连板', '涨停', '跌停', '一进二', '二进三', '三进四',
    '首板', '二板', '三板', '四板', '五板',
    '强势股', '弱势股', '热门股',
    '昨日连板', '昨日涨停', '今日连板', '今日涨停',
    '昨日首板', '今日首板', '昨日强势', '今日强势',
    '昨日热门', '今日热门', '昨日异动', '今日异动'
]

def should_filter_concept(concept_name):
    """判断是否应该过滤掉某个概念板块"""
    concept_name_lower = concept_name.lower()
    for keyword in FILTERED_CONCEPT_KEYWORDS:
        if keyword in concept_name:
            return True
    return False

# 飞书机器人的Webhook URL
feishu_webhook_url = 'https://open.feishu.cn/open-apis/bot/v2/hook/186eaf03-826f-4793-ab8f-c9f2d9149482'

# 飞书应用凭证（需要创建飞书应用并开通上传图片权限）
# 注意：这些需要在飞书开放平台创建应用后获取
FEISHU_APP_ID = "cli_a37c6ffbdxxxxxxx"  # 请替换为实际的app_id
FEISHU_APP_SECRET = "mLstZkv0C4d1sxxxxxxxxxxxxxxx"  # 请替换为实际的app_secret

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

# 全局缓存变量
_all_concepts_cache = None
_concept_stocks_cache = {}

def get_tenant_access_token():
    """获取飞书应用的tenant_access_token"""
    try:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }
        payload_data = {
            "app_id": "cli_a8c1546693bf501c",
            "app_secret": "mUuiI1fpQ9k1GUmHH4dghbY74AVCSNBk"
        }
        response = requests.post(url=url, json=payload_data, headers=headers)
        result = response.json()
        
        if result.get('code') == 0:
            return result.get('tenant_access_token')
        else:
            print(f"获取token失败: {result}")
            return None
    except Exception as e:
        print(f"获取token异常: {e}")
        return None

def upload_image_to_feishu(image_path):
    """上传图片到飞书并获取image_key"""
    try:
        # 获取token
        token = get_tenant_access_token()
        if not token:
            return None
        
        # 上传图片
        url = "https://open.feishu.cn/open-apis/im/v1/images"
        headers = {
            'Authorization': f'Bearer {token}',
        }
        
        with open(image_path, 'rb') as f:
            files = {
                'image_type': (None, 'message'),
                'image': (os.path.basename(image_path), f, 'image/png')
            }
            response = requests.post(url, headers=headers, files=files)
        
        result = response.json()
        if result.get('code') == 0:
            return result['data']['image_key']
        else:
            print(f"上传图片失败: {result}")
            return None
            
    except Exception as e:
        print(f"上传图片异常: {e}")
        return None

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

def send_feishu_image(image_key):
    """发送图片到飞书群"""
    try:
        headers = {
            'Content-Type': 'application/json'
        }
        data = {
            "msg_type": "image",
            "content": {
                "image_key": image_key
            }
        }
        response = requests.post(feishu_webhook_url, json=data, headers=headers)
        return response.json()
    except Exception as e:
        print(f"发送图片到飞书失败: {e}")
        return None

def text_to_image(text, width=1400, font_size=18, line_spacing=10):
    """将文字转换为图片"""
    try:
        # 尝试使用系统字体 - 增加更多Linux服务器字体路径
        font_paths = [
            "/System/Library/Fonts/PingFang.ttc",  # macOS 中文字体
            "/System/Library/Fonts/Helvetica.ttc",  # macOS 英文字体
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",  # Linux
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",  # Linux 中文
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux 中文
            "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",  # Linux 中文
            "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",  # Linux 中文
            "C:/Windows/Fonts/msyh.ttc",  # Windows 微软雅黑
            "C:/Windows/Fonts/arial.ttf",  # Windows Arial
        ]
        
        font = None
        for font_path in font_paths:
            if os.path.exists(font_path):
                try:
                    font = ImageFont.truetype(font_path, font_size)
                    break
                except:
                    continue
        
        if font is None:
            font = ImageFont.load_default()
        
        # 处理文本，移除emoji和特殊字符，保留中文和基本符号
        clean_text = ""
        for char in text:
            # 保留中文、英文、数字、基本标点符号
            if (ord(char) < 127 or  # ASCII字符
                '\u4e00' <= char <= '\u9fff' or  # 中文字符
                char in '，。！？：；""''（）【】《》、·—…'):
                clean_text += char
            elif char in '\n\t':  # 保留换行和制表符
                clean_text += char
            else:
                clean_text += ' '  # 其他字符替换为空格
        
        # 按行分割文本
        lines = clean_text.split('\n')
        wrapped_lines = []
        
        # 计算每行可容纳的字符数（根据字体大小调整）
        chars_per_line = max(60, width // (font_size * 0.6))  # 更精确的字符数计算
        
        for line in lines:
            if len(line) <= chars_per_line:
                wrapped_lines.append(line)
            else:
                # 对长行进行换行处理，保持中文完整性
                if any('\u4e00' <= char <= '\u9fff' for char in line):
                    # 包含中文的行，按字符数切分
                    while len(line) > chars_per_line:
                        wrapped_lines.append(line[:chars_per_line])
                        line = line[chars_per_line:]
                    if line:
                        wrapped_lines.append(line)
                else:
                    # 纯英文行，使用textwrap
                    wrapped = textwrap.fill(line, width=int(chars_per_line))
                    wrapped_lines.extend(wrapped.split('\n'))
        
        # 计算图片高度
        line_height = font_size + line_spacing
        height = len(wrapped_lines) * line_height + 60  # 增加上下边距
        
        # 创建图片，使用浅灰色背景
        img = Image.new('RGB', (width, height), color='#f8f9fa')
        draw = ImageDraw.Draw(img)
        
        # 绘制边框
        draw.rectangle([5, 5, width-5, height-5], outline='#dee2e6', width=2)
        
        # 绘制标题背景
        title_height = 50
        draw.rectangle([10, 10, width-10, title_height], fill='#e9ecef', outline='#ced4da')
        
        # 绘制标题
        title = "股票分析报告"
        title_font_size = font_size + 4
        try:
            title_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", title_font_size)
        except:
            title_font = font
        
        # 计算标题居中位置
        title_bbox = draw.textbbox((0, 0), title, font=title_font)
        title_width = title_bbox[2] - title_bbox[0]
        title_x = (width - title_width) // 2
        draw.text((title_x, 20), title, font=title_font, fill='#495057')
        
        # 绘制文字内容
        y = title_height + 20  # 标题下方开始
        for line in wrapped_lines:
            # 根据内容类型选择颜色
            if '===' in line:  # 分节标题
                color = '#007bff'
                font_weight = font
            elif line.strip().startswith('【') and ')】' in line:  # 股票名称
                color = '#28a745'
                font_weight = font
            elif '涨跌幅:+' in line:  # 上涨
                color = '#dc3545'
                font_weight = font
            elif '涨跌幅:-' in line:  # 下跌
                color = '#28a745'
                font_weight = font
            else:
                color = '#212529'
                font_weight = font
            
            draw.text((25, y), line, font=font_weight, fill=color)  # 左边距25
            y += line_height
        
        return img
        
    except Exception as e:
        print(f"文字转图片失败: {e}")
        return None


# 只注释掉调用，不动函数定义
# def save_image_to_local(image, filename="stock_analysis.png"):
#     """保存图片到本地文件"""
#     try:
#         # 保存图片到当前目录
#         image.save(filename, format='PNG')
#         print(f"📁 图片已保存到: {os.path.abspath(filename)}")
#         return {"status": "success", "filename": filename}
#         
#     except Exception as e:
#         print(f"❌ 保存图片失败: {e}")
#         return None


def send_feishu_message_and_image(content):
    """发送文字消息到飞书，并生成图片发送到飞书"""
    # 发送文字消息
    text_response = send_feishu_message(content)
    print("文字消息已发送到飞书")
    
    # 生成图片
    print("正在生成图片...")
    image = text_to_image(content)
    
    if image:
        # 保存图片到本地
        # timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        # filename = f"stock_analysis_{timestamp}.png"
        # save_result = save_image_to_local(image, filename)
        
        if False:  # 如果需要保存图片，请取消注释上面的save_image_to_local调用
            # 保存图片到本地
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"stock_analysis_{timestamp}.png"
            save_result = save_image_to_local(image, filename)
            
            if save_result:
                print("图片已保存到本地")
            
                # 尝试上传并发送图片到飞书
                print("正在上传图片到飞书...")
                image_key = upload_image_to_feishu(filename)
                
                if image_key:
                    print(f"图片上传成功，image_key: {image_key}")
                    img_response = send_feishu_image(image_key)
                    if img_response:
                        print("图片已发送到飞书群")
                        return text_response, img_response
                    else:
                        print("图片发送到飞书失败")
                        return text_response, None
                else:
                    print("图片上传到飞书失败，请检查飞书应用配置")
                    print("提示：需要在飞书开放平台创建应用并配置正确的app_id和app_secret")
                    return text_response, None
            else:
                print("图片保存失败")
                return text_response, None
        else:
            print("图片生成失败，仅发送文字消息")
            return text_response, None
    else:
        print("图片生成失败，仅发送文字消息")
        return text_response, None


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


def get_all_concepts():
    """获取所有概念板块名称（带缓存）"""
    global _all_concepts_cache
    if _all_concepts_cache is not None:
        return _all_concepts_cache
    
    try:
        _all_concepts_cache = ak.stock_board_concept_name_em()
        return _all_concepts_cache
    except Exception as e:
        # print(f"获取概念板块数据时出错: {e}")  # 注释掉错误打印
        return pd.DataFrame()


def get_concept_stocks(concept_name):
    """获取指定概念板块的所有股票（带缓存）"""
    global _concept_stocks_cache
    if concept_name in _concept_stocks_cache:
        return _concept_stocks_cache[concept_name]
    
    try:
        concept_stocks = ak.stock_board_concept_cons_em(symbol=concept_name)
        _concept_stocks_cache[concept_name] = concept_stocks
        return concept_stocks
    except Exception as e:
        # print(f"获取概念 {concept_name} 股票数据时出错: {e}")  # 注释掉错误打印
        _concept_stocks_cache[concept_name] = pd.DataFrame()
        return pd.DataFrame()


def get_stock_fund_flow(stock_code):
    """获取个股的资金流向数据"""
    try:
        # 判断市场
        if stock_code.startswith('6'):
            market = 'sh'
        elif stock_code.startswith('0') or stock_code.startswith('3'):
            market = 'sz'
        else:
            return 0
            
        # 获取个股资金流向
        stock_fund_flow = ak.stock_individual_fund_flow(stock=stock_code, market=market)
        if not stock_fund_flow.empty:
            # 获取最新一天的主力净流入数据
            latest_data = stock_fund_flow.iloc[-1]  # 最新一条记录
            return float(latest_data.get('主力净流入-净额', 0))
    except Exception as e:
        # print(f"获取股票 {stock_code} 资金流向数据时出错: {e}")  # 注释掉错误打印
        pass
    return 0


def get_stock_concepts_with_performance_optimized(stock_codes):
    """批量获取多只股票所属的概念及其涨幅信息（优化版）"""
    all_concepts = get_all_concepts()
    if all_concepts.empty:
        return pd.DataFrame()
    
    # 性能优化：只检查按涨幅排名前100的概念板块
    top_concepts = all_concepts.sort_values('涨跌幅', ascending=False).head(100)
    
    all_stock_concepts = []
    stock_codes_set = {code for code, _ in stock_codes}  # 转换为set提高查找效率
    
    print(f"正在分析 {len(stock_codes)} 只股票的概念分布...")
    print(f"性能优化：只检查涨幅前100的概念板块（共{len(all_concepts)}个概念）...")
    
    # 遍历前100个概念，找出哪些股票属于哪些概念
    processed_concepts = 0
    found_relations = 0
    filtered_concepts = 0
    
    for i, concept_row in top_concepts.iterrows():
        concept_name = concept_row['板块名称']
        processed_concepts += 1
        
        # 过滤掉临时性概念板块
        if should_filter_concept(concept_name):
            filtered_concepts += 1
            continue
        
        # 每处理20个概念显示一次进度
        if processed_concepts % 20 == 0:
            print(f"已处理概念: {processed_concepts}/100, 找到关联: {found_relations}, 已过滤: {filtered_concepts}")
        
        try:
            stocks = get_concept_stocks(concept_name)
            if not stocks.empty and '代码' in stocks.columns:
                # 使用set intersection提高效率
                concept_stock_codes = set(stocks['代码'].values)
                matching_stocks = stock_codes_set.intersection(concept_stock_codes)
                
                if matching_stocks:
                    # 为每个匹配的股票创建概念信息
                    for stock_code in matching_stocks:
                        # 找到对应的股票名称
                        stock_name = next(name for code, name in stock_codes if code == stock_code)
                        
                        concept_info = {
                            '股票代码': stock_code,
                            '股票名称': stock_name,
                            '概念名称': concept_name,
                            '概念涨跌幅': float(concept_row.get('涨跌幅', 0)),
                            '概念最新价': concept_row.get('最新价', 0),
                            '概念涨跌额': concept_row.get('涨跌额', 0),
                            '概念总市值': concept_row.get('总市值', 0),
                            '概念换手率': concept_row.get('换手率', 0),
                            '概念上涨家数': concept_row.get('上涨家数', 0),
                            '概念下跌家数': concept_row.get('下跌家数', 0),
                            '概念领涨股票': concept_row.get('领涨股票', ''),
                            '概念领涨股票涨跌幅': concept_row.get('领涨股票-涨跌幅', 0)
                        }
                        all_stock_concepts.append(concept_info)
                        found_relations += 1
        except Exception as e:
            continue
    
    print(f"概念分析完成！总共找到 {found_relations} 个股票-概念关联，过滤了 {filtered_concepts} 个临时性概念")
    
    # 转换为DataFrame并按涨跌幅排序
    if all_stock_concepts:
        concepts_df = pd.DataFrame(all_stock_concepts)
        concepts_df = concepts_df.sort_values('概念涨跌幅', ascending=False)
        return concepts_df
    else:
        return pd.DataFrame()


def analyze_filtered_stocks_concepts(filtered_stocks):
    """分析筛选出的股票的概念分布（优化版）"""
    if not filtered_stocks:
        return pd.DataFrame()
    
    # 使用优化的批量处理函数
    print(f"开始批量分析 {len(filtered_stocks)} 只股票的概念分布...")
    concepts_df = get_stock_concepts_with_performance_optimized(filtered_stocks)
    
    return concepts_df


def get_concept_limit_up_count_cached(concept_name, concept_stocks_cache=None):
    """获取概念板块的涨停家数（使用缓存的概念股票数据）"""
    if concept_stocks_cache and concept_name in concept_stocks_cache:
        concept_stocks = concept_stocks_cache[concept_name]
    else:
        concept_stocks = get_concept_stocks(concept_name)
    
    if concept_stocks.empty:
        return 0
    
    try:
        # 计算涨停家数（涨跌幅>=9.8%）
        limit_up_count = 0
        if '涨跌幅' in concept_stocks.columns:
            limit_up_stocks = concept_stocks[concept_stocks['涨跌幅'] >= 9.8]
            limit_up_count = len(limit_up_stocks)
        return limit_up_count
    except Exception as e:
        return 0


def format_stock_concepts_message(filtered_stocks, concepts_df):
    """按照指定格式组织股票概念信息"""
    if concepts_df.empty:
        return "未找到概念信息"
    
    message = ""
    
    # 获取实时数据用于显示股票价格信息
    real_time_data = get_real_time_data()
    
    # 预先计算所有需要的涨停家数，避免重复调用
    unique_concepts = concepts_df['概念名称'].unique()
    print(f"预先计算 {len(unique_concepts)} 个概念的涨停家数...")
    limit_up_cache = {}
    for concept_name in unique_concepts:
        limit_up_cache[concept_name] = get_concept_limit_up_count_cached(concept_name)
    
    # 按股票分组显示概念信息
    for stock_code, stock_name in filtered_stocks:
        stock_concepts = concepts_df[concepts_df['股票代码'] == stock_code]
        
        if not stock_concepts.empty:
            # 获取股票的实时价格信息
            stock_info = real_time_data[real_time_data['代码'] == stock_code]
            if not stock_info.empty:
                current_price = float(stock_info.iloc[0]['最新价'])
                open_price = float(stock_info.iloc[0]['今开'])
                pct_change = float(stock_info.iloc[0]['涨跌幅'])
                market_cap = float(stock_info.iloc[0]['总市值'])
                
                pct_str = f"+{pct_change:.1f}%" if pct_change >= 0 else f"{pct_change:.1f}%"
                market_cap_yi = market_cap / 10**8  # 转换为亿元
                message += f"【{stock_name}({stock_code})】涨跌幅:{pct_str}, 当前价:{current_price:.2f}, 开盘价:{open_price:.2f}, 总市值:{market_cap_yi:.1f}亿\n"
            else:
                message += f"【{stock_name}({stock_code})】\n"
            
            message += f"  所属概念：\n"
            
            # 按涨跌幅排序显示概念
            stock_concepts_sorted = stock_concepts.sort_values('概念涨跌幅', ascending=False)
            
            for idx, (_, concept) in enumerate(stock_concepts_sorted.iterrows(), 1):
                concept_name = concept['概念名称']
                concept_change = concept['概念涨跌幅']
                
                # 获取概念统计信息
                up_count = int(concept.get('概念上涨家数', 0))
                down_count = int(concept.get('概念下跌家数', 0))
                leader_stock = concept.get('概念领涨股票', '')
                leader_change = concept.get('概念领涨股票涨跌幅', 0)
                
                # 使用缓存的涨停家数
                limit_up_count = limit_up_cache.get(concept_name, 0)
                
                # 概念涨跌幅格式化
                if concept_change >= 0:
                    change_str = f"+{concept_change:.1f}%"
                else:
                    change_str = f"{concept_change:.1f}%"
                
                # 添加统计信息
                total_stocks = up_count + down_count
                up_rate = (up_count / total_stocks * 100) if total_stocks > 0 else 0
                down_rate = (down_count / total_stocks * 100) if total_stocks > 0 else 0
                limit_up_rate = (limit_up_count / total_stocks * 100) if total_stocks > 0 else 0
                
                stats_info = f"            上涨{up_count}家({up_rate:.1f}%)"
                if limit_up_count > 0:
                    stats_info += f", 涨停{limit_up_count}家({limit_up_rate:.1f}%)"
                stats_info += f", 下跌{down_count}家({down_rate:.1f}%)"
                
                if leader_stock and leader_change != 0:
                    leader_change_str = f"+{leader_change:.1f}%" if leader_change >= 0 else f"{leader_change:.1f}%"
                    stats_info += f", 领涨: 【{leader_stock}】({leader_change_str})"
                
                concept_info = f"          {idx}.【{concept_name}】(涨跌幅: {change_str})\n"
                concept_info += stats_info + "\n"
                message += concept_info
            
            message += "\n"
    
    return message


def generate_concept_summary_with_stats(concepts_df):
    """生成带详细统计信息的概念汇总"""
    if concepts_df.empty:
        return pd.DataFrame()
    
    # 概念统计，保留详细信息
    concept_stats = concepts_df.groupby('概念名称').agg({
        '概念涨跌幅': 'first',  # 每个概念的涨跌幅
        '概念上涨家数': 'first',  # 上涨家数
        '概念下跌家数': 'first',  # 下跌家数
        '概念领涨股票': 'first',  # 领涨股票
        '概念领涨股票涨跌幅': 'first',  # 领涨股票涨跌幅
        '股票代码': 'count',    # 该概念下筛选出的股票数量
        '股票名称': lambda x: ', '.join(x)  # 股票名称列表
    }).reset_index()
    
    concept_stats.columns = ['概念名称', '概念涨跌幅', '上涨家数', '下跌家数', '领涨股票', '领涨股票涨跌幅', '筛选股票数量', '股票列表']
    concept_stats = concept_stats.sort_values('概念涨跌幅', ascending=False)
    
    return concept_stats


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


def get_concept_top_stocks(concept_name, top_n=10):
    """获取概念板块的前N只股票，过滤并排序"""
    try:
        concept_stocks = get_concept_stocks(concept_name)
        if concept_stocks.empty:
            return []
        
        # 过滤掉创业板(30开头)、科创板(688开头)、ST股票
        filtered_stocks = []
        for _, stock in concept_stocks.iterrows():
            code = stock.get('代码', '')
            name = stock.get('名称', '')
            
            # 过滤条件
            if (code.startswith('30') or code.startswith('688') or 
                'ST' in name or 'st' in name.lower()):
                continue
            
            # 获取股票信息
            stock_info = {
                '代码': code,
                '名称': name,
                '涨跌幅': float(stock.get('涨跌幅', 0)),
                '最新价': float(stock.get('最新价', 0)),
                '成交量': float(stock.get('成交量', 0)),
                '成交额': float(stock.get('成交额', 0)),
                '总市值': stock.get('总市值', 0),
                '流通市值': stock.get('流通市值', 0)
            }
            filtered_stocks.append(stock_info)
        
        # 排序：先按涨跌幅降序，涨跌幅相同时按成交额降序（成交额大的在前，表示更活跃）
        def sort_key(stock):
            pct_change = stock['涨跌幅']
            volume_amount = stock['成交额']  # 成交额作为次要排序条件
            return (-pct_change, -volume_amount)  # 都用负号表示降序
        
        filtered_stocks.sort(key=sort_key)
        
        # 返回前N只
        return filtered_stocks[:top_n]
        
    except Exception as e:
        # print(f"获取概念 {concept_name} 股票详情时出错: {e}")
        return []


def get_hot_stocks(top_n=20):
    """获取最热门的N只主板股票"""
    try:
        hot_stocks_df = ak.stock_hot_rank_em()
        if hot_stocks_df.empty:
            return pd.DataFrame()
        
        # 过滤只保留主板股票（6开头的沪市主板，0开头的深市主板）
        # 排除创业板(30开头)、科创板(688开头)、北交所(8开头、43开头)
        main_board_stocks = []
        for _, stock in hot_stocks_df.iterrows():
            code = str(stock.get('代码', ''))
            name = str(stock.get('名称', ''))
            
            # 只保留主板股票：6开头(沪市主板)、00开头(深市主板)
            if (code.startswith('6') or code.startswith('00')) and 'ST' not in name:
                main_board_stocks.append(stock)
        
        # 转换为DataFrame并取前N只
        if main_board_stocks:
            main_board_df = pd.DataFrame(main_board_stocks)
            return main_board_df.head(top_n)
        else:
            return pd.DataFrame()
        
    except Exception as e:
        print(f"获取热门股票数据时出错: {e}")
        return pd.DataFrame()


def format_hot_stocks_message(hot_stocks_df):
    """格式化热门股票信息"""
    if hot_stocks_df.empty:
        return "未获取到热门主板股票数据"
    
    message = ""
    
    for idx, (_, stock) in enumerate(hot_stocks_df.iterrows(), 1):
        # 获取股票基本信息
        stock_code = str(stock.get('代码', ''))
        stock_name = str(stock.get('名称', ''))
        current_price = float(stock.get('最新价', 0))
        pct_change = float(stock.get('涨跌幅', 0))
        volume = stock.get('成交量', 0)
        amount = float(stock.get('成交额', 0))
        
        # 格式化涨跌幅
        pct_str = f"+{pct_change:.1f}%" if pct_change >= 0 else f"{pct_change:.1f}%"
        
        # 格式化成交额（转换为万元或亿元）
        if amount >= 10**8:  # 大于1亿
            amount_str = f"{amount/10**8:.1f}亿"
        elif amount >= 10**4:  # 大于1万
            amount_str = f"{amount/10**4:.0f}万"
        else:
            amount_str = f"{amount:.0f}"
        
        # 构建股票信息 - 显示完整的股票名称和代码，使用【】格式
        message += f"{idx:2d}. 【{stock_name}({stock_code})】\n"
        message += f"        价格: ¥{current_price:.2f} | 涨跌: {pct_str} | 成交额: {amount_str}\n"
        
        # 每5只股票空一行，便于阅读
        if idx % 5 == 0 and idx < len(hot_stocks_df):
            message += "\n"
    
    return message


if __name__ == '__main__':
    # 记录开始时间
    start_time = datetime.now()
    print(f"程序开始执行时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    real_time_data = get_real_time_data()
    filtered_stocks = filter_stocks(real_time_data)
    print(f"筛选出的股票数量: {len(filtered_stocks)}")

    if len(filtered_stocks) > 0:
        # 分析概念分布
        concepts_df = analyze_filtered_stocks_concepts(filtered_stocks)
        
        if not concepts_df.empty:
            # 使用新的格式化函数
            formatted_concepts_message = format_stock_concepts_message(filtered_stocks, concepts_df)
            
            # 生成概念汇总（用于内部分析）
            concept_summary = generate_concept_summary_with_stats(concepts_df)
            
            # 构建发送到飞书的完整消息
            feishu_message = f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            feishu_message += f"满足条件的股票数量：{len(filtered_stocks)}只\n\n"
            feishu_message += "=== 股票概念分析 ===\n"
            feishu_message += formatted_concepts_message
            
            # 添加热门股票模块
            print(f"获取最热门的10只主板股票...")
            hot_stocks_df = get_hot_stocks(10)
            if not hot_stocks_df.empty:
                hot_stocks_message = format_hot_stocks_message(hot_stocks_df)
                feishu_message += "\n=== 今日最热门主板股票TOP10 ===\n"
                feishu_message += hot_stocks_message
            
            # 添加热门概念汇总
            if not concept_summary.empty:
                feishu_message += "\n=== 热门概念汇总 ===\n"
                feishu_message += "前5个涨幅最高的概念板块：\n"
                
                # 直接获取所有概念板块数据并排序
                all_concepts = get_all_concepts()
                if not all_concepts.empty:
                    # 过滤掉临时性概念板块
                    filtered_concepts = []
                    for _, concept_row in all_concepts.iterrows():
                        concept_name = concept_row['板块名称']
                        if not should_filter_concept(concept_name):
                            filtered_concepts.append(concept_row)
                    
                    if filtered_concepts:
                        # 转换为DataFrame并按涨跌幅排序，取前5个
                        filtered_concepts_df = pd.DataFrame(filtered_concepts)
                        top_concepts = filtered_concepts_df.sort_values('涨跌幅', ascending=False).head(5)
                        
                        # 预先计算前5个概念的涨停家数
                        top_concept_names = top_concepts['板块名称'].tolist()
                        print(f"计算前5个热门概念的涨停家数和股票详情...")
                        top_limit_up_cache = {}
                        for concept_name in top_concept_names:
                            top_limit_up_cache[concept_name] = get_concept_limit_up_count_cached(concept_name)
                        
                        for idx, (_, concept_row) in enumerate(top_concepts.iterrows(), 1):
                            concept_name = concept_row['板块名称']
                            concept_change = float(concept_row.get('涨跌幅', 0))
                            up_count = int(concept_row.get('上涨家数', 0))
                            down_count = int(concept_row.get('下跌家数', 0))
                            leader_stock = concept_row.get('领涨股票', '')
                            leader_change = float(concept_row.get('领涨股票-涨跌幅', 0))
                            
                            # 获取概念板块的实际股票总数
                            total_stocks = up_count + down_count
                            
                            # 使用缓存的涨停家数
                            limit_up_count = top_limit_up_cache.get(concept_name, 0)
                            
                            change_str = f"+{concept_change:.1f}%" if concept_change >= 0 else f"{concept_change:.1f}%"
                            
                            # 基本信息 - 显示概念板块总股票数，概念名称用【】格式
                            feishu_message += f"{idx}. 【{concept_name}】: {change_str} (共{total_stocks}只股票)\n"
                            
                            # 详细统计信息
                            up_rate = (up_count / total_stocks * 100) if total_stocks > 0 else 0
                            down_rate = (down_count / total_stocks * 100) if total_stocks > 0 else 0
                            limit_up_rate = (limit_up_count / total_stocks * 100) if total_stocks > 0 else 0
                            
                            stats_info = f"            上涨{up_count}家({up_rate:.1f}%)"
                            if limit_up_count > 0:
                                stats_info += f", 涨停{limit_up_count}家({limit_up_rate:.1f}%)"
                            stats_info += f", 下跌{down_count}家({down_rate:.1f}%)"
                            
                            if leader_stock and leader_change != 0:
                                leader_change_str = f"+{leader_change:.1f}%" if leader_change >= 0 else f"{leader_change:.1f}%"
                                stats_info += f", 领涨: 【{leader_stock}】({leader_change_str})"
                            
                            feishu_message += stats_info + "\n"
                            
                            # 根据排名决定显示的股票数量：前3个概念显示10只，其余显示5只
                            stock_count = 10 if idx <= 3 else 5
                            top_stocks = get_concept_top_stocks(concept_name, stock_count)
                            if top_stocks:
                                stock_label = "前10只股票" if idx <= 3 else "前5只股票"
                                feishu_message += f"             {stock_label}：\n"
                                for stock_idx, stock in enumerate(top_stocks, 1):
                                    stock_code = stock['代码']
                                    stock_name = stock['名称']
                                    stock_change = stock['涨跌幅']
                                    stock_price = stock['最新价']
                                    
                                    stock_change_str = f"+{stock_change:.1f}%" if stock_change >= 0 else f"{stock_change:.1f}%"
                                    
                                    feishu_message += f"                {stock_idx:2d}. 【{stock_name}({stock_code})】{stock_change_str} ¥{stock_price:.2f}\n"
                            
                            feishu_message += "\n"  # 概念之间空一行
            
            # 计算运行耗时
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = duration.total_seconds()
            
            # 输出最终消息和运行耗时
            print(f"\n{feishu_message}")
            print(f"\n程序执行完成时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"总运行耗时: {duration_seconds:.2f}秒")
            
            # 发送到飞书（不包含耗时信息）
            send_feishu_message_and_image(feishu_message)
            
        else:
            message = f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n满足条件的股票如下：\n"
            for stock in filtered_stocks:
                message += f"\t 【{stock[1]}({stock[0]})】\n"
            message += "\n未找到相关概念信息"
            
            # 计算运行耗时
            end_time = datetime.now()
            duration = end_time - start_time
            duration_seconds = duration.total_seconds()
            
            print(f"\n{message}")
            print(f"\n程序执行完成时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"总运行耗时: {duration_seconds:.2f}秒")
            
            send_feishu_message_and_image(message)
    else:
        message = "没有找到满足条件的股票"
        
        # 计算运行耗时
        end_time = datetime.now()
        duration = end_time - start_time
        duration_seconds = duration.total_seconds()
        
        print(f"\n{message}")
        print(f"\n程序执行完成时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"总运行耗时: {duration_seconds:.2f}秒")
        
        send_feishu_message_and_image(message)
