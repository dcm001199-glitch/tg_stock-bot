import sqlite3
from collections import defaultdict
from datetime import datetime, time as dtime

import yfinance as yf
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========= 配置 =========
BOT_TOKEN = "8543904501:AAGmptuQNpejBS4Y-rE6lkQPTS9f80qbU7I"   # ← 换成你的 Token
DB_PATH = "watchlist.db"        # 新数据库文件
MOVE_THRESHOLD = 3.0            # 全局默认盘中异动阈值（百分比）
LAST_PRICES: dict[str, float] = {}  # 用于盘中异动判断
# ========================


# ========= 数据库相关 =========
DB_CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
DB_CONN.row_factory = sqlite3.Row


def init_db():
    cur = DB_CONN.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol  TEXT    NOT NULL,
            tp      REAL    NOT NULL,
            sl      REAL    NOT NULL,
            active  INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchlist_symbol ON watchlist(symbol)"
    )
    DB_CONN.commit()


def add_watch(user_id: int, symbol: str, tp: float, sl: float):
    cur = DB_CONN.cursor    ()
    cur.execute(
        "INSERT INTO watchlist (user_id, symbol, tp, sl, active) "
        "VALUES (?, ?, ?, ?, 1)",
        (user_id, symbol, tp, sl),
    )
    DB_CONN.commit()


def get_user_watches(user_id: int):
    cur = DB_CONN.cursor()
    cur.execute(
        "SELECT symbol, tp, sl FROM watchlist "
        "WHERE user_id = ? AND active = 1 "
        "ORDER BY symbol",
        (user_id,),
    )
    return cur.fetchall()


def remove_watch(user_id: int, symbol: str) -> int:
    cur = DB_CONN.cursor()
    cur.execute(
        "UPDATE watchlist SET active = 0 "
        "WHERE user_id = ? AND symbol = ? AND active = 1",
        (user_id, symbol.upper()),
    )
    DB_CONN.commit()
    return cur.rowcount


def get_all_active_watches():
    cur = DB_CONN.cursor()
    cur.execute(
        "SELECT user_id, symbol, tp, sl FROM watchlist "
        "WHERE active = 1"
    )
    return cur.fetchall()


# ========= 行情获取 =========
def get_price(symbol: str):
    """盘中用：取最近一根 1 分钟 K 的收盘价"""
    try:
        data = yf.Ticker(symbol).history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception:
        return None


def get_daily_snapshot(symbol: str):
    """
    收盘总结用：
    period=2d, interval=1d 取最近两天 K 线，算收盘价 & 日涨跌幅 & 当日高低
    """
    try:
        data = yf.Ticker(symbol).history(period="2d", interval="1d")
        if data.empty:
            return None

        last_close = float(data["Close"].iloc[-1])
        day_high = float(data["High"].iloc[-1])
        day_low = float(data["Low"].iloc[-1])

        if len(data) >= 2:
            prev_close = float(data["Close"].iloc[-2])
            if prev_close > 0:
                change_pct = (last_close - prev_close) / prev_close * 100
            else:
                change_pct = 0.0
        else:
            change_pct = 0.0

        return {
            "last": last_close,
            "high": day_high,
            "low": day_low,
            "change_pct": change_pct,
        }
    except Exception:
        return None


# ========= 机器人命令 =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📈 股票监控机器人（SQLite 专业版）\n\n"
        "常用命令：\n"
        "/add AAPL 185 160  → 添加监控（代码、止盈、止损）\n"
        "/list              → 查看当前监控列表\n"
        "/remove AAPL       → 删除某只股票的监控\n"
        "/setmove 3         → 设置盘中异动阈值为 3%\n\n"
        "系统功能：\n"
        "· 每分钟检查价格，触发止盈 / 止损 / 盘中异动提醒\n"
        "· 每天美东 16:05 自动推送「今日监控总结」"
    )


async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) != 3:
        await update.message.reply_text("格式错误！正确示例：/add AAPL 185 160")
        return

    symbol = args[0].upper()
    try:
        tp = float(args[1])
        sl = float(args[2])
    except ValueError:
        await update.message.reply_text("止盈 / 止损必须是数字，例如：/add AAPL 185 160")
        return

    add_watch(user_id, symbol, tp, sl)

    await update.message.reply_text(
        f"✅ 已添加监控：\n"
        f"股票：{symbol}\n"
        f"止盈：{tp}\n"
        f"止损：{sl}\n"
        f"我会每分钟检查价格，并在触发止盈 / 止损或盘中异动时提醒你。"
    )


async def list_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_watches(user_id)

    if not rows:
        await update.message.reply_text("你当前没有任何监控记录，用 /add AAPL 185 160 添加一条试试。")
        return

    lines = ["📋 当前监控列表："]
    for r in rows:
        lines.append(
            f"- {r['symbol']}: 止盈 {r['tp']}, 止损 {r['sl']}"
        )

    await update.message.reply_text("\n".join(lines))


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) != 1:
        await update.message.reply_text("用法：/remove AAPL")
        return

    symbol = args[0].upper()
    affected = remove_watch(user_id, symbol)

    if affected > 0:
        await update.message.reply_text(f"已删除 {symbol} 的监控记录。")
    else:
        await update.message.reply_text(f"你当前没有监控 {symbol}。")


async def set_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MOVE_THRESHOLD
    args = context.args

    if len(args) != 1:
        await update.message.reply_text("用法：/setmove 3   （设置盘中异动阈值为 3%）")
        return

    try:
        value = float(args[0])
    except ValueError:
        await update.message.reply_text("请输入数字，例如：/setmove 2 或 /setmove 5")
        return

    if value <= 0:
        await update.message.reply_text("阈值必须大于 0。")
        return

    MOVE_THRESHOLD = value
    await update.message.reply_text(f"✅ 已将盘中异动阈值设置为：{MOVE_THRESHOLD:.2f}%")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "请使用命令操作，例如：\n"
        "/add AAPL 185 160\n"
        "/list\n"
        "/remove AAPL\n"
        "/setmove 3"
    )


# ========= 定时任务：盘中每分钟检查 =========
async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_active_watches()
    if not rows:
        return

    # 按 symbol 去重，减少请求次数
    symbols = sorted({r["symbol"] for r in rows})
    prices: dict[str, float] = {}

    for sym in symbols:
        price = get_price(sym)
        if price is not None:
            prices[sym] = price

    for r in rows:
        user_id = r["user_id"]
        sym = r["symbol"]
        tp = r["tp"]
        sl = r["sl"]

        price = prices.get(sym)
        if price is None:
            continue

        messages: list[str] = []

        # ① 止盈 / 止损
        if price >= tp:
            messages.append(
                f"🎯 止盈提醒\n{sym} 当前价格：{price:.2f} ≥ 你的止盈价 {tp:.2f}"
            )
        if price <= sl:
            messages.append(
                f"⚠️ 止损提醒\n{sym} 当前价格：{price:.2f} ≤ 你的止损价 {sl:.2f}"
            )

        # ② 盘中异动
        last_price = LAST_PRICES.get(sym)
        if last_price is not None and last_price > 0:
            change_pct = (price - last_price) / last_price * 100
            if abs(change_pct) >= MOVE_THRESHOLD:
                direction = "上涨" if change_pct > 0 else "下跌"
                messages.append(
                    f"🚨 盘中异动提醒\n"
                    f"{sym} 约 1 分钟内{direction}了 {change_pct:.2f}%\n"
                    f"当前价格：{price:.2f}"
                )
                LAST_PRICES[sym] = price
        else:
            LAST_PRICES[sym] = price

        for text in messages:
            try:
                await context.bot.send_message(chat_id=user_id, text=text)
            except Exception:
                pass


# ========= 定时任务：每日收盘总结（美东 16:05） =========
async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_active_watches()
    if not rows:
        return

    # 按用户分组
    user_map: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        user_map[r["user_id"]].append(r)

    today_str = datetime.now().strftime("%Y-%m-%d")

    for user_id, stocks in user_map.items():
        lines: list[str] = []
        lines.append("【今日监控总结 | 内部版】")
        lines.append(f"日期：{today_str}（美东）")
        lines.append(f"监控股票数量：{len(stocks)}")
        lines.append("")

        lines.append("个股明细：")

        idx = 1
        for r in stocks:
            sym = r["symbol"]
            tp = r["tp"]
            sl = r["sl"]

            snap = get_daily_snapshot(sym)
            if snap is None:
                continue

            last = snap["last"]
            high = snap["high"]
            low = snap["low"]
            chg = snap["change_pct"]

            hit_tp = high >= tp
            hit_sl = low <= sl

            lines.append(f"{idx}. {sym}")
            lines.append(f"  收盘价：{last:.2f}")
            lines.append(f"  当日涨跌幅：{chg:+.2f}%")
            lines.append(f"  日内区间：{low:.2f} - {high:.2f}")
            lines.append(f"  止盈：{tp:.2f}（{'触及' if hit_tp else '未触及'}）")
            lines.append(f"  止损：{sl:.2f}（{'触及' if hit_sl else '未触及'}）")
            lines.append("")
            idx += 1

        text = "\n".join(lines)

        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception:
            pass


# ========= 设置命令菜单 =========
async def post_init(app):
    commands = [
        BotCommand("start", "查看使用说明"),
        BotCommand("add", "添加监控：/add 代码 止盈 止损"),
        BotCommand("list", "查看当前监控列表"),
        BotCommand("remove", "删除某只股票监控"),
        BotCommand("setmove", "设置盘中异动阈值"),
    ]
    await app.bot.set_my_commands(commands)


# ========= 主程序 =========
def main():
    init_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # 命令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_stock))
    app.add_handler(CommandHandler("list", list_watch))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("setmove", set_move))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # 定时任务：盘中每 60 秒检查一次
    job_queue = app.job_queue
    job_queue.run_repeating(check_prices, interval=60, first=10)

    # 每天美东时间 16:05 推送收盘总结（电脑本地时间就是美东）
    job_queue.run_daily(
        send_daily_summary,
        time=dtime(hour=16, minute=5),
    )

    print("机器人已启动（SQLite 版），正在监控股票并计划每日收盘总结...")
    app.run_polling()


if __name__ == "__main__":
    main()
