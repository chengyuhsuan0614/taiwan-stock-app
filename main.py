# 台灣股市 API 伺服器
# 功能：提供股票資料給網頁和手機 App 使用

import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import quote_plus

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# 備用清單：當證交所/櫃買中心暫時連不上時，至少常用股仍會顯示中文。
FALLBACK_STOCK_NAMES = {
    "1101": "台泥",
    "1102": "亞泥",
    "1216": "統一",
    "1301": "台塑",
    "1303": "南亞",
    "1326": "台化",
    "2002": "中鋼",
    "2049": "上銀",
    "2207": "和泰車",
    "2301": "光寶科",
    "2303": "聯電",
    "2308": "台達電",
    "2317": "鴻海",
    "2327": "國巨",
    "2330": "台積電",
    "2357": "華碩",
    "2379": "瑞昱",
    "2382": "廣達",
    "2395": "研華",
    "2408": "南亞科",
    "2412": "中華電",
    "2454": "聯發科",
    "2603": "長榮",
    "2609": "陽明",
    "2615": "萬海",
    "2880": "華南金",
    "2881": "富邦金",
    "2882": "國泰金",
    "2883": "開發金",
    "2884": "玉山金",
    "2885": "元大金",
    "2886": "兆豐金",
    "2887": "台新金",
    "2890": "永豐金",
    "2891": "中信金",
    "2892": "第一金",
    "2912": "統一超",
    "3008": "大立光",
    "3034": "聯詠",
    "3045": "台灣大",
    "3231": "緯創",
    "3661": "世芯-KY",
    "3680": "家登",
    "3711": "日月光投控",
    "4904": "遠傳",
    "5871": "中租-KY",
    "5880": "合庫金",
    "6505": "台塑化",
    "6669": "緯穎",
}


STOCK_LIST_URLS = [
    # 上市公司基本資料
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "https://dts.twse.com.tw/opendata/t187ap03_L.csv",
    # 上櫃公司基本資料
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_O",
    "https://dts.twse.com.tw/opendata/t187ap03_O.csv",
]


TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"


def clean_stock_name(name: str) -> str:
    name = str(name or "").strip()
    remove_words = [
        "股份有限公司",
        "股份有限",
        "有限公司",
        "公司",
    ]
    for word in remove_words:
        name = name.replace(word, "")
    return name.strip() or "查無資料"


def pick_name(row: dict) -> str:
    for field_name in ["公司簡稱", "股票簡稱", "簡稱", "公司名稱", "有價證券名稱"]:
        value = row.get(field_name)
        if value:
            return clean_stock_name(value)
    return ""


def parse_stock_rows(text: str) -> dict[str, str]:
    stock_names = {}
    rows = []

    try:
        parsed_json = requests.models.complexjson.loads(text)
        if isinstance(parsed_json, list):
            rows = parsed_json
    except ValueError:
        csv_text = text.lstrip("\ufeff")
        rows = list(csv.DictReader(io.StringIO(csv_text)))

    for row in rows:
        code = str(row.get("公司代號") or row.get("股票代號") or row.get("證券代號") or "").strip()
        name = pick_name(row)
        if code and name:
            stock_names[code] = name

    return stock_names


@lru_cache(maxsize=1)
def load_stock_name_map() -> dict[str, str]:
    stock_names = {}

    for url in STOCK_LIST_URLS:
        try:
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            stock_names.update(parse_stock_rows(response.text))
        except requests.RequestException:
            continue

    # 備用清單放最後，避免官方資料缺簡稱時仍有漂亮名稱。
    stock_names.update(FALLBACK_STOCK_NAMES)
    return stock_names


def get_chinese_name(stock_code: str, info: dict) -> str:
    name_map = load_stock_name_map()
    if stock_code in name_map:
        return name_map[stock_code]
    return info.get("shortName") or info.get("longName") or "查無資料"


def to_float(value, default=0):
    try:
        if value is None:
            return default
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def to_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


def parse_twse_number(value) -> int:
    return to_int(value, 0)


def resolve_ticker(stock_code: str):
    """
    台股 Yahoo Finance 格式：
    上市通常是 2330.TW，上櫃通常是 8069.TWO。
    這裡會自動兩邊都試，讓新增股票比較不會卡住。
    美股則直接使用 AAPL / TSLA / NVDA 這種原始代號。
    """
    stock_code = stock_code.strip().upper()

    if any(ch.isalpha() for ch in stock_code):
        ticker = yf.Ticker(stock_code)
        history = ticker.history(period="5d")
        if not history.empty:
            return ticker, stock_code, "美股", history
        raise HTTPException(status_code=404, detail=f"找不到股票代號 {stock_code}")

    for suffix, market in [(".TW", "上市"), (".TWO", "上櫃")]:
        symbol = f"{stock_code}{suffix}"
        ticker = yf.Ticker(symbol)
        try:
            history = ticker.history(period="5d")
            if not history.empty:
                return ticker, symbol, market, history
        except Exception:
            continue
    raise HTTPException(status_code=404, detail=f"找不到股票代號 {stock_code}")


def get_quote(stock_code: str) -> dict:
    ticker, symbol, market, history = resolve_ticker(stock_code)
    try:
        info = ticker.info
    except Exception:
        info = {}

    last_row = history.iloc[-1] if not history.empty else None
    prev_row = history.iloc[-2] if len(history) >= 2 else last_row

    price = to_float(info.get("currentPrice")) or to_float(last_row["Close"] if last_row is not None else 0)
    prev_close = to_float(info.get("previousClose")) or to_float(prev_row["Close"] if prev_row is not None else 0)
    open_price = to_float(info.get("open")) or to_float(last_row["Open"] if last_row is not None else 0)
    high = to_float(info.get("dayHigh")) or to_float(last_row["High"] if last_row is not None else 0)
    low = to_float(info.get("dayLow")) or to_float(last_row["Low"] if last_row is not None else 0)
    volume = to_int(info.get("volume")) or to_int(last_row["Volume"] if last_row is not None else 0)

    return {
        "ticker": ticker,
        "symbol": symbol,
        "market": market,
        "info": info,
        "currency": info.get("currency", "TWD" if market in ["上市", "上櫃"] else "USD"),
        "history": history,
        "price": price,
        "prev_close": prev_close,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
    }


def build_daily_history(history) -> list[dict]:
    history_list = []
    for date, row in history.iterrows():
        history_list.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": to_float(row["Open"]),
            "high": to_float(row["High"]),
            "low": to_float(row["Low"]),
            "close": to_float(row["Close"]),
            "volume": to_int(row["Volume"])
        })
    return history_list


def build_intraday_history(ticker) -> list[dict]:
    intraday = ticker.history(period="1d", interval="1m")
    points = []
    for date, row in intraday.iterrows():
        close = to_float(row["Close"])
        if close <= 0:
            continue
        points.append({
            "time": date.strftime("%H:%M"),
            "price": close,
            "open": to_float(row["Open"]),
            "high": to_float(row["High"]),
            "low": to_float(row["Low"]),
            "volume": to_int(row["Volume"]),
        })
    return points


def build_history(ticker, period: str, interval: str) -> list[dict]:
    history = ticker.history(period=period, interval=interval)
    points = []
    for date, row in history.iterrows():
        close = to_float(row["Close"])
        if close <= 0:
            continue
        label = date.strftime("%m/%d") if interval == "1d" else date.strftime("%H:%M")
        points.append({
            "time": label,
            "price": close,
            "open": to_float(row["Open"]),
            "high": to_float(row["High"]),
            "low": to_float(row["Low"]),
            "volume": to_int(row["Volume"]),
        })
    return points


def get_news_items(symbol: str) -> list[dict]:
    if symbol.endswith(".TW") or symbol.endswith(".TWO"):
        return get_tw_yahoo_news(symbol)

    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(symbol)}&region=US&lang=en-US"
    try:
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except Exception:
        return []

    items = []
    for item in root.findall("./channel/item")[:8]:
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "source": "Yahoo Finance",
        })
    return items


def get_tw_yahoo_news(symbol: str) -> list[dict]:
    url = f"https://tw.stock.yahoo.com/quote/{quote_plus(symbol)}/news"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = []
    seen = set()
    for link in soup.find_all("a", href=True):
        title = " ".join(link.get_text(" ", strip=True).split())
        href = link["href"]
        if len(title) < 12:
            continue
        if title in seen:
            continue
        if "news" not in href and "tw.news.yahoo.com" not in href:
            continue
        if href.startswith("/"):
            href = f"https://tw.stock.yahoo.com{href}"
        items.append({
            "title": title,
            "link": href,
            "published": "",
            "source": "Yahoo股市",
        })
        seen.add(title)
        if len(items) >= 8:
            break
    return items


def get_company_events(ticker) -> list[dict]:
    events = []

    try:
        calendar = ticker.calendar
        if isinstance(calendar, dict):
            for key, value in calendar.items():
                events.append({
                    "type": str(key),
                    "date": str(value),
                    "note": "Yahoo Finance calendar",
                })
        elif hasattr(calendar, "empty") and not calendar.empty:
            for key, value in calendar.to_dict().items():
                events.append({
                    "type": str(key),
                    "date": str(value),
                    "note": "Yahoo Finance calendar",
                })
    except Exception:
        pass

    try:
        earnings_dates = ticker.get_earnings_dates(limit=4)
        if hasattr(earnings_dates, "iterrows"):
            for date, row in earnings_dates.iterrows():
                events.append({
                    "type": "財報/法說相關日期",
                    "date": date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date),
                    "note": "Yahoo Finance earnings calendar",
                })
    except Exception:
        pass

    if not events:
        events.append({
            "type": "股東會 / 法說會",
            "date": "目前資料源未提供",
            "note": "之後可再串接公開資訊觀測站完整資料",
        })

    return events[:8]


def recent_weekdays(days_back: int = 45) -> list[str]:
    today = datetime.utcnow() + timedelta(hours=8)
    dates = []
    for i in range(days_back):
        day = today - timedelta(days=i)
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
    return dates


def twse_json(url: str, params: dict) -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, params=params, headers=headers, timeout=8)
    response.raise_for_status()
    return response.json()


def row_to_dict(fields: list[str], row: list) -> dict:
    return {fields[i]: row[i] for i in range(min(len(fields), len(row)))}


def get_t86_for_date(date: str, stock_code: str) -> dict | None:
    data = twse_json(TWSE_T86_URL, {
        "date": date,
        "selectType": "ALLBUT0999",
        "response": "json",
    })
    fields = data.get("fields", [])
    for row in data.get("data", []):
        row_map = row_to_dict(fields, row)
        if str(row_map.get("證券代號", "")).strip() == stock_code:
            return {
                "foreign": parse_twse_number(row_map.get("外陸資買賣超股數(不含外資自營商)")),
                "investment_trust": parse_twse_number(row_map.get("投信買賣超股數")),
            }
    return None


def get_margin_for_date(date: str, stock_code: str) -> dict | None:
    data = twse_json(TWSE_MARGIN_URL, {
        "date": date,
        "selectType": "ALL",
        "response": "json",
    })
    fields = data.get("fields", [])
    for row in data.get("data", []):
        row_map = row_to_dict(fields, row)
        if str(row_map.get("股票代號", row_map.get("證券代號", ""))).strip() == stock_code:
            return {
                "margin_balance": parse_twse_number(row_map.get("今日餘額", row_map.get("融資今日餘額"))),
                "margin_change": parse_twse_number(row_map.get("增減", row_map.get("融資增減"))),
            }
    return None


def get_twse_flow_trend(stock_code: str) -> list[dict]:
    points = []
    for date in recent_weekdays():
        if len(points) >= 20:
            break
        try:
            t86 = get_t86_for_date(date, stock_code) or {}
            margin = get_margin_for_date(date, stock_code) or {}
        except Exception:
            continue

        if not t86 and not margin:
            continue

        points.append({
            "date": f"{date[4:6]}/{date[6:8]}",
            "foreign": t86.get("foreign", 0),
            "investment_trust": t86.get("investment_trust", 0),
            "margin_balance": margin.get("margin_balance", 0),
            "margin_change": margin.get("margin_change", 0),
        })

    return list(reversed(points))


def get_financial_trends(stock_code: str, quote: dict) -> dict:
    revenue_points = get_tw_monthly_revenue(stock_code, quote["symbol"])
    quarterly = get_yfinance_quarterly_financials(quote["ticker"])
    return {
        "revenue": revenue_points,
        "gross_margin": quarterly["gross_margin"],
        "eps": quarterly["eps"],
        "message": "月營收優先抓 Yahoo 台股；毛利率/EPS 取 Yahoo Finance 季資料。資料源不足時會留空。",
    }


def get_tw_monthly_revenue(stock_code: str, symbol: str) -> list[dict]:
    if not (symbol.endswith(".TW") or symbol.endswith(".TWO")):
        return []

    url = f"https://tw.stock.yahoo.com/quote/{quote_plus(symbol)}/revenue"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
    except Exception:
        return []

    text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
    tokens = text.replace(",", "").split()
    points = []

    for idx, token in enumerate(tokens):
        if "/" not in token:
            continue
        parts = token.split("/")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        year, month = parts
        if len(year) not in [3, 4] or len(month) not in [1, 2]:
            continue
        for next_token in tokens[idx + 1: idx + 8]:
            value = to_int(next_token, None)
            if value and value > 0:
                points.append({
                    "date": token,
                    "value": value,
                })
                break
        if len(points) >= 12:
            break

    return list(reversed(points[:12]))


def get_yfinance_quarterly_financials(ticker) -> dict:
    gross_margin = []
    eps = []
    try:
        financials = ticker.quarterly_financials
        income_stmt = ticker.quarterly_income_stmt
        df = income_stmt if hasattr(income_stmt, "empty") and not income_stmt.empty else financials
        if hasattr(df, "empty") and not df.empty:
            for col in list(df.columns)[:4]:
                label = col.strftime("%Y/%m") if hasattr(col, "strftime") else str(col)[:7]
                total_revenue = to_float(df.loc["Total Revenue", col] if "Total Revenue" in df.index else 0)
                gross_profit = to_float(df.loc["Gross Profit", col] if "Gross Profit" in df.index else 0)
                net_income = to_float(df.loc["Net Income", col] if "Net Income" in df.index else 0)
                shares = to_float(df.loc["Diluted Average Shares", col] if "Diluted Average Shares" in df.index else 0)
                if total_revenue and gross_profit:
                    gross_margin.append({"date": label, "value": round(gross_profit / total_revenue * 100, 2)})
                if net_income and shares:
                    eps.append({"date": label, "value": round(net_income / shares, 2)})
    except Exception:
        pass

    return {
        "gross_margin": list(reversed(gross_margin)),
        "eps": list(reversed(eps)),
    }


# 建立 API 伺服器
app = FastAPI()

# 允許網頁連線（很重要，沒有這個網頁會被擋住）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 第一個 API：測試伺服器是否正常運作
@app.get("/")
def home():
    return {"message": "Taiwan Stock API is running"}


# 查詢股票中文名稱清單載入狀態
@app.get("/stock-name/{stock_code}")
def stock_name(stock_code: str):
    name = load_stock_name_map().get(stock_code)
    try:
        quote = get_quote(stock_code)
        name = get_chinese_name(stock_code, quote["info"])
        market = quote["market"]
        symbol = quote["symbol"]
    except HTTPException:
        market = ""
        symbol = ""

    return {
        "code": stock_code,
        "name": name or "查無資料",
        "market": market,
        "symbol": symbol,
    }


# 第二個 API：查詢單一股票資料
# 使用方式：瀏覽器輸入 http://localhost:8000/stock/2330
@app.get("/stock/{stock_code}")
def get_stock(stock_code: str):
    quote = get_quote(stock_code)
    info = quote["info"]

    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "market": quote["market"],
        "currency": quote["currency"],
        "name": get_chinese_name(stock_code, info),
        "price": quote["price"],
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "prev_close": quote["prev_close"],
        "volume": quote["volume"],
        "pe_ratio": info.get("trailingPE", 0) or 0,
        "history": build_daily_history(quote["history"])
    }


@app.get("/stock/{stock_code}/intraday")
def get_intraday(stock_code: str):
    quote = get_quote(stock_code)
    points = build_intraday_history(quote["ticker"])

    if points:
        quote["price"] = points[-1]["price"]

    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "market": quote["market"],
        "currency": quote["currency"],
        "name": get_chinese_name(stock_code, quote["info"]),
        "price": quote["price"],
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "prev_close": quote["prev_close"],
        "volume": quote["volume"],
        "points": points,
        "updated_at": points[-1]["time"] if points else "",
    }


@app.get("/stock/{stock_code}/history")
def get_stock_history(stock_code: str, period: str = "30d", interval: str = "1d"):
    quote = get_quote(stock_code)
    points = build_history(quote["ticker"], period, interval)

    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "market": quote["market"],
        "currency": quote["currency"],
        "name": get_chinese_name(stock_code, quote["info"]),
        "price": quote["price"],
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "prev_close": quote["prev_close"],
        "volume": quote["volume"],
        "points": points,
        "period": period,
        "interval": interval,
    }


@app.get("/stock/{stock_code}/news")
def get_stock_news(stock_code: str):
    quote = get_quote(stock_code)
    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "name": get_chinese_name(stock_code, quote["info"]),
        "news": get_news_items(quote["symbol"]),
    }


@app.get("/stock/{stock_code}/events")
def get_stock_events(stock_code: str):
    quote = get_quote(stock_code)
    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "name": get_chinese_name(stock_code, quote["info"]),
        "events": get_company_events(quote["ticker"]),
    }


@app.get("/stock/{stock_code}/flows")
def get_stock_flows(stock_code: str):
    quote = get_quote(stock_code)

    if quote["market"] not in ["上市", "上櫃"]:
        return {
            "code": stock_code,
            "symbol": quote["symbol"],
            "market": quote["market"],
            "available": False,
            "message": "外資/投信/融資資料目前僅支援台股。",
            "points": [],
        }

    if quote["market"] == "上櫃":
        return {
            "code": stock_code,
            "symbol": quote["symbol"],
            "market": quote["market"],
            "available": False,
            "message": "上櫃法人/融資資料需另接 TPEx，下一版可補。",
            "points": [],
        }

    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "market": quote["market"],
        "available": True,
        "message": "資料來源：TWSE，通常為盤後資料。",
        "points": get_twse_flow_trend(stock_code),
    }


@app.get("/stock/{stock_code}/financials")
def get_stock_financials(stock_code: str):
    quote = get_quote(stock_code)
    return {
        "code": stock_code,
        "symbol": quote["symbol"],
        "market": quote["market"],
        "name": get_chinese_name(stock_code, quote["info"]),
        **get_financial_trends(stock_code, quote),
    }


# 第三個 API：一次查詢多支股票
# 使用方式：http://localhost:8000/watchlist/2330,2303,2454
@app.get("/watchlist/{codes}")
def get_watchlist(codes: str):
    stock_list = codes.split(",")
    result = []

    for code in stock_list:
        try:
            quote = get_quote(code)
        except HTTPException:
            result.append({
                "code": code,
                "name": "查無資料",
                "price": 0,
                "change": 0,
                "change_pct": 0,
                "volume": 0,
                "market": "",
                "currency": "",
                "error": True,
            })
            continue

        info = quote["info"]
        prev = quote["prev_close"]
        current = quote["price"]
        change = round(current - prev, 2)
        change_pct = round((change / prev * 100), 2) if prev else 0

        result.append({
            "code": code,
            "symbol": quote["symbol"],
            "market": quote["market"],
            "currency": quote["currency"],
            "name": get_chinese_name(code, info),
            "price": current,
            "change": change,
            "change_pct": change_pct,
            "volume": info.get("volume", 0),
        })

    return {"stocks": result}
