import logging
import os
from datetime import datetime, date, timedelta
from io import BytesIO

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Load environment variables from .env if present
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWM_API_KEY = os.getenv("OWM_API_KEY")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("water_calorie_bot")

USERS = {}

# Conversation states
(
    WEIGHT,
    HEIGHT,
    AGE,
    SEX,
    ACTIVITY,
    CITY,
    MANUAL_CALORIES,
    CALORIES_VALUE,
) = range(8)

(
    FOOD_NAME,
    FOOD_KCAL_MANUAL,
    FOOD_GRAMS,
) = range(3)


def get_user(user_id: int) -> dict:
    user = USERS.setdefault(
        user_id,
        {
            "weight": None,
            "height": None,
            "age": None,
            "sex": None,
            "activity": None,
            "city": None,
            "manual_calorie_goal": None,
            "logged_water": 0,
            "logged_calories": 0,
            "burned_calories": 0,
            "history": [],
            "last_temp": None,
            "last_temp_ts": None,
            "last_date": date.today().isoformat(),
        },
    )

    today = date.today().isoformat()
    if user.get("last_date") != today:
        user["logged_water"] = 0
        user["logged_calories"] = 0
        user["burned_calories"] = 0
        user["history"] = []
        user["last_date"] = today
    return user


def parse_float(value: str):
    try:
        return float(value.replace(",", "."))
    except Exception:
        return None


def parse_int(value: str):
    try:
        return int(float(value.replace(",", ".")))
    except Exception:
        return None


def normalize_sex(text: str):
    t = text.strip().lower()
    if t in {"m", "male", "м", "муж", "мужчина"}:
        return "male"
    if t in {"f", "female", "ж", "жен", "женщина"}:
        return "female"
    return None


def calc_water_goal(weight: float, activity: int, temp_c: float | None) -> int:
    if not weight:
        return 0
    base = weight * 30
    activity_bonus = (activity // 30) * 500 if activity else 0
    heat_bonus = 0
    if temp_c is not None:
        if temp_c > 30:
            heat_bonus = 1000
        elif temp_c > 25:
            heat_bonus = 500
    return int(base + activity_bonus + heat_bonus)


def calc_calorie_goal(weight: float, height: int, age: int, sex: str, activity: int) -> int:
    if not all([weight, height, age]):
        return 0
    s = 5 if sex == "male" else -161 if sex == "female" else 0
    bmr = 10 * weight + 6.25 * height - 5 * age + s
    activity_bonus = (activity // 30) * 100 if activity else 0
    return int(bmr + activity_bonus)


def get_temperature(city: str, user: dict):
    if not city or not OWM_API_KEY:
        return None
    now = datetime.utcnow()
    last_ts = user.get("last_temp_ts")
    if last_ts and now - last_ts < timedelta(minutes=30):
        return user.get("last_temp")

    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": OWM_API_KEY, "units": "metric"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            temp = data.get("main", {}).get("temp")
            if temp is not None:
                user["last_temp"] = float(temp)
                user["last_temp_ts"] = now
                return float(temp)
    except Exception as exc:
        logger.warning("Weather fetch failed: %s", exc)
    return None


def fetch_food_kcal(product_name: str):
    url = "https://world.openfoodfacts.org/cgi/search.pl"
    try:
        resp = requests.get(
            url,
            params={
                "action": "process",
                "search_terms": product_name,
                "json": "true",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        products = data.get("products", [])
        if not products:
            return None

        for p in products:
            nutr = p.get("nutriments", {})
            kcal = nutr.get("energy-kcal_100g")
            if kcal is None:
                energy_100g = nutr.get("energy_100g")
                unit = nutr.get("energy_unit") or nutr.get("energy-unit")
                if energy_100g is not None and unit:
                    if str(unit).lower() == "kj":
                        kcal = float(energy_100g) / 4.184
                    elif str(unit).lower() in {"kcal", "cal"}:
                        kcal = float(energy_100g)
            if kcal is not None:
                name = p.get("product_name") or p.get("product_name_ru") or product_name
                return {
                    "name": name,
                    "kcal_per_100g": float(kcal),
                }
        return None
    except Exception as exc:
        logger.warning("Food fetch failed: %s", exc)
        return None


def ensure_profile(user: dict):
    return all([user.get("weight"), user.get("height"), user.get("age"), user.get("activity")])


def log_history(user: dict, kind: str, amount: float):
    user["history"].append(
        {
            "ts": datetime.utcnow().isoformat(),
            "kind": kind,
            "amount": amount,
        }
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу рассчитать норму воды и калорий и вести трекинг. "
        "Начните с /set_profile. Для справки: /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Доступные команды:\n"
        "/set_profile — настройка профиля\n"
        "/profile — показать текущий профиль\n"
        "/log_water <мл> — записать воду\n"
        "/log_food <продукт> — записать еду\n"
        "/log_workout <тип> <мин> — записать тренировку\n"
        "/check_progress — прогресс по воде и калориям\n"
        "/plot — графики прогресса\n"
        "/recommend — рекомендации\n"
        "/reset_day — сбросить дневные логи"
    )


async def set_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите ваш вес (в кг):")
    return WEIGHT


async def set_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    weight = parse_float(update.message.text)
    if not weight or weight <= 0:
        await update.message.reply_text("Некорректный вес. Введите число в кг:")
        return WEIGHT
    user = get_user(update.effective_user.id)
    user["weight"] = weight
    await update.message.reply_text("Введите ваш рост (в см):")
    return HEIGHT


async def set_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    height = parse_int(update.message.text)
    if not height or height <= 0:
        await update.message.reply_text("Некорректный рост. Введите число в см:")
        return HEIGHT
    user = get_user(update.effective_user.id)
    user["height"] = height
    await update.message.reply_text("Введите ваш возраст:")
    return AGE


async def set_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    age = parse_int(update.message.text)
    if not age or age <= 0:
        await update.message.reply_text("Некорректный возраст. Введите число:")
        return AGE
    user = get_user(update.effective_user.id)
    user["age"] = age
    await update.message.reply_text("Укажите пол (м/ж), можно пропустить и ввести '-' :")
    return SEX


async def set_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    sex = normalize_sex(text) if text != "-" else None
    user = get_user(update.effective_user.id)
    user["sex"] = sex
    await update.message.reply_text("Сколько минут активности в день?")
    return ACTIVITY


async def set_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activity = parse_int(update.message.text)
    if activity is None or activity < 0:
        await update.message.reply_text("Некорректное значение. Введите минуты:")
        return ACTIVITY
    user = get_user(update.effective_user.id)
    user["activity"] = activity
    await update.message.reply_text("В каком городе вы находитесь?")
    return CITY


async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    user = get_user(update.effective_user.id)
    user["city"] = city
    await update.message.reply_text("Хотите задать цель калорий вручную? (да/нет)")
    return MANUAL_CALORIES


async def set_manual_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in {"да", "yes", "y"}:
        await update.message.reply_text("Введите цель калорий (ккал):")
        return CALORIES_VALUE
    user = get_user(update.effective_user.id)
    user["manual_calorie_goal"] = None
    await update.message.reply_text("Профиль сохранен! Используйте /check_progress.")
    return ConversationHandler.END


async def set_manual_calories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = parse_int(update.message.text)
    if not value or value <= 0:
        await update.message.reply_text("Некорректно. Введите число ккал:")
        return CALORIES_VALUE
    user = get_user(update.effective_user.id)
    user["manual_calorie_goal"] = value
    await update.message.reply_text("Профиль сохранен! Используйте /check_progress.")
    return ConversationHandler.END


async def cancel_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Настройка профиля отменена.")
    return ConversationHandler.END


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Профиль не заполнен. Используйте /set_profile.")
        return

    temp = get_temperature(user.get("city"), user)
    water_goal = calc_water_goal(user["weight"], user["activity"], temp)
    calorie_goal = user.get("manual_calorie_goal") or calc_calorie_goal(
        user["weight"], user["height"], user["age"], user.get("sex"), user["activity"]
    )

    await update.message.reply_text(
        "Ваш профиль:\n"
        f"Вес: {user['weight']} кг\n"
        f"Рост: {user['height']} см\n"
        f"Возраст: {user['age']}\n"
        f"Активность: {user['activity']} мин/день\n"
        f"Город: {user['city']}\n"
        f"Норма воды: {water_goal} мл\n"
        f"Норма калорий: {calorie_goal} ккал"
    )


async def log_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Сначала заполните профиль: /set_profile")
        return
    if not context.args:
        await update.message.reply_text("Использование: /log_water <мл>")
        return
    amount = parse_int(context.args[0])
    if not amount or amount <= 0:
        await update.message.reply_text("Введите количество воды в мл.")
        return

    user["logged_water"] += amount
    log_history(user, "water", amount)

    temp = get_temperature(user.get("city"), user)
    water_goal = calc_water_goal(user["weight"], user["activity"], temp)
    remaining = max(water_goal - user["logged_water"], 0)
    await update.message.reply_text(
        f"Записано: {amount} мл. Осталось до нормы: {remaining} мл."
    )


async def log_food_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Сначала заполните профиль: /set_profile")
        return ConversationHandler.END

    if context.args:
        name = " ".join(context.args).strip()
        context.user_data["food_name"] = name
        food = fetch_food_kcal(name)
        if food:
            context.user_data["food_kcal"] = food["kcal_per_100g"]
            await update.message.reply_text(
                f"{food['name']} — {food['kcal_per_100g']:.1f} ккал на 100 г. "
                "Сколько грамм вы съели?"
            )
            return FOOD_GRAMS

        await update.message.reply_text(
            "Не удалось найти калорийность. Введите ккал на 100 г вручную:"
        )
        return FOOD_KCAL_MANUAL

    await update.message.reply_text("Введите название продукта:")
    return FOOD_NAME


async def log_food_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["food_name"] = name
    food = fetch_food_kcal(name)
    if food:
        context.user_data["food_kcal"] = food["kcal_per_100g"]
        await update.message.reply_text(
            f"{food['name']} — {food['kcal_per_100g']:.1f} ккал на 100 г. "
            "Сколько грамм вы съели?"
        )
        return FOOD_GRAMS

    await update.message.reply_text(
        "Не удалось найти калорийность. Введите ккал на 100 г вручную:"
    )
    return FOOD_KCAL_MANUAL


async def log_food_kcal_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kcal = parse_float(update.message.text)
    if kcal is None or kcal <= 0:
        await update.message.reply_text("Введите число ккал на 100 г:")
        return FOOD_KCAL_MANUAL
    context.user_data["food_kcal"] = kcal
    await update.message.reply_text("Сколько грамм вы съели?")
    return FOOD_GRAMS


async def log_food_grams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    grams = parse_float(update.message.text)
    if grams is None or grams <= 0:
        await update.message.reply_text("Введите количество грамм:")
        return FOOD_GRAMS

    user = get_user(update.effective_user.id)
    kcal_per_100g = context.user_data.get("food_kcal")
    name = context.user_data.get("food_name")
    if kcal_per_100g is None:
        await update.message.reply_text("Не удалось записать продукт. Попробуйте снова.")
        return ConversationHandler.END

    consumed = kcal_per_100g * grams / 100.0
    user["logged_calories"] += consumed
    log_history(user, "food", consumed)

    await update.message.reply_text(
        f"Записано: {name} — {consumed:.1f} ккал."
    )
    context.user_data.pop("food_kcal", None)
    context.user_data.pop("food_name", None)
    return ConversationHandler.END


async def log_food_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("food_kcal", None)
    context.user_data.pop("food_name", None)
    await update.message.reply_text("Логирование еды отменено.")
    return ConversationHandler.END


WORKOUT_KCAL_PER_MIN = {
    "бег": 10,
    "ходьба": 4,
    "велосипед": 7,
    "плавание": 8,
    "силовая": 6,
    "йога": 3,
}


async def log_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Сначала заполните профиль: /set_profile")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /log_workout <тип> <мин>\n"
            "Напр.: /log_workout бег 30"
        )
        return
    workout_type = " ".join(context.args[:-1]).lower()
    minutes = parse_int(context.args[-1])
    if not minutes or minutes <= 0:
        await update.message.reply_text("Введите длительность в минутах.")
        return

    kcal_per_min = WORKOUT_KCAL_PER_MIN.get(workout_type, 6)
    burned = kcal_per_min * minutes
    user["burned_calories"] += burned
    log_history(user, "workout", burned)

    extra_water = (minutes // 30) * 200
    if extra_water:
        await update.message.reply_text(
            f"🏃 {workout_type} {minutes} мин — {burned} ккал. "
            f"Дополнительно: выпейте {extra_water} мл воды."
        )
    else:
        await update.message.reply_text(
            f"🏃 {workout_type} {minutes} мин — {burned} ккал."
        )


async def check_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Сначала заполните профиль: /set_profile")
        return

    temp = get_temperature(user.get("city"), user)
    water_goal = calc_water_goal(user["weight"], user["activity"], temp)
    calorie_goal = user.get("manual_calorie_goal") or calc_calorie_goal(
        user["weight"], user["height"], user["age"], user.get("sex"), user["activity"]
    )

    water_left = max(water_goal - user["logged_water"], 0)
    calories_left = max(calorie_goal - user["logged_calories"], 0)
    balance = user["logged_calories"] - user["burned_calories"]

    temp_note = f"Температура: {temp:.1f}°C\n" if temp is not None else ""

    await update.message.reply_text(
        "📊 Прогресс:\n"
        f"{temp_note}"
        "Вода:\n"
        f"- Выпито: {int(user['logged_water'])} мл из {water_goal} мл.\n"
        f"- Осталось: {int(water_left)} мл.\n\n"
        "Калории:\n"
        f"- Потреблено: {int(user['logged_calories'])} ккал из {calorie_goal} ккал.\n"
        f"- Сожжено: {int(user['burned_calories'])} ккал.\n"
        f"- Баланс: {int(balance)} ккал.\n"
        f"- Осталось: {int(calories_left)} ккал."
    )


async def plot_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import matplotlib.pyplot as plt

    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Сначала заполните профиль: /set_profile")
        return

    temp = get_temperature(user.get("city"), user)
    water_goal = calc_water_goal(user["weight"], user["activity"], temp)
    calorie_goal = user.get("manual_calorie_goal") or calc_calorie_goal(
        user["weight"], user["height"], user["age"], user.get("sex"), user["activity"]
    )

    history = user.get("history", [])
    times = []
    water = []
    calories = []

    w_total = 0
    c_total = 0
    for h in history:
        ts = datetime.fromisoformat(h["ts"])
        if h["kind"] == "water":
            w_total += h["amount"]
        elif h["kind"] == "food":
            c_total += h["amount"]
        elif h["kind"] == "workout":
            c_total -= h["amount"]
        times.append(ts)
        water.append(w_total)
        calories.append(c_total)

    if not times:
        await update.message.reply_text("Пока нет данных для графика. Запишите воду/еду/тренировки.")
        return

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6))
    ax1.plot(times, water, label="Вода (мл)")
    ax1.axhline(water_goal, color="green", linestyle="--", label="Цель")
    ax1.set_title("Прогресс воды")
    ax1.set_ylabel("мл")
    ax1.legend()

    ax2.plot(times, calories, color="orange", label="Калорийный баланс")
    ax2.axhline(calorie_goal, color="green", linestyle="--", label="Цель")
    ax2.set_title("Прогресс калорий")
    ax2.set_ylabel("ккал")
    ax2.legend()

    fig.autofmt_xdate()
    buf = BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)

    await update.message.reply_photo(photo=buf, caption="Графики прогресса")


async def recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ensure_profile(user):
        await update.message.reply_text("Сначала заполните профиль: /set_profile")
        return

    temp = get_temperature(user.get("city"), user)
    water_goal = calc_water_goal(user["weight"], user["activity"], temp)
    calorie_goal = user.get("manual_calorie_goal") or calc_calorie_goal(
        user["weight"], user["height"], user["age"], user.get("sex"), user["activity"]
    )

    water_left = max(water_goal - user["logged_water"], 0)
    calorie_left = max(calorie_goal - user["logged_calories"], 0)

    low_calorie_foods = [
        "огурец (15 ккал/100г)",
        "помидор (18 ккал/100г)",
        "яблоко (52 ккал/100г)",
        "кефир 1% (40 ккал/100г)",
        "куриная грудка (165 ккал/100г)",
    ]
    workouts = [
        "ходьба 30 мин (≈120 ккал)",
        "бег 20 мин (≈200 ккал)",
        "йога 40 мин (≈120 ккал)",
        "велосипед 30 мин (≈210 ккал)",
    ]

    msg = ["Рекомендации:"]
    if water_left > 0:
        msg.append(f"- Выпейте ещё ~{int(min(water_left, 500))} мл воды.")
    if calorie_left > 0:
        msg.append(f"- Осталось {int(calorie_left)} ккал: выбирайте лёгкие продукты.")
    else:
        msg.append("- Вы превысили цель по калориям: добавьте активность.")
    msg.append("- Идеи продуктов: " + ", ".join(low_calorie_foods))
    msg.append("- Идеи тренировок: " + ", ".join(workouts))

    await update.message.reply_text("\n".join(msg))


async def reset_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    user["logged_water"] = 0
    user["logged_calories"] = 0
    user["burned_calories"] = 0
    user["history"] = []
    user["last_date"] = date.today().isoformat()
    await update.message.reply_text("Дневные логи сброшены.")


async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        logger.info("User %s: %s", update.effective_user.id, update.message.text)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    profile_conv = ConversationHandler(
        entry_points=[CommandHandler("set_profile", set_profile)],
        states={
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_weight)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_height)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_age)],
            SEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_sex)],
            ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_activity)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_city)],
            MANUAL_CALORIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_manual_choice)],
            CALORIES_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_manual_calories)],
        },
        fallbacks=[CommandHandler("cancel", cancel_profile)],
    )

    food_conv = ConversationHandler(
        entry_points=[CommandHandler("log_food", log_food_start)],
        states={
            FOOD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_food_name)],
            FOOD_KCAL_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_food_kcal_manual)],
            FOOD_GRAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_food_grams)],
        },
        fallbacks=[CommandHandler("cancel", log_food_cancel)],
    )

    app.add_handler(profile_conv)
    app.add_handler(food_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("profile", show_profile))
    app.add_handler(CommandHandler("log_water", log_water))
    app.add_handler(CommandHandler("log_workout", log_workout))
    app.add_handler(CommandHandler("check_progress", check_progress))
    app.add_handler(CommandHandler("plot", plot_progress))
    app.add_handler(CommandHandler("recommend", recommend))
    app.add_handler(CommandHandler("reset_day", reset_day))

    app.add_handler(MessageHandler(filters.ALL, log_all_updates), group=-1)

    app.run_polling()


if __name__ == "__main__":
    main()
