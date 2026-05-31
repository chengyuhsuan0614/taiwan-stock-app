# 台灣股市 API 伺服器
# 功能：提供股票資料給網頁和手機 App 使用

import csv
import io
from functools import lru_cache

import requests
import yfinance as yf
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
        return int(value)
    except (TypeError, ValueError):
        return default


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
