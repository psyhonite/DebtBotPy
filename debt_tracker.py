import os
import csv
import logging
import sqlite3
import tempfile
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ApplicationBuilder,
)
from dotenv import load_dotenv

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Constants (Conversation States) ---
(
    STATE_IDLE,
    STATE_ADDING_DEBTOR_NAME,
    STATE_ADDING_DEBT_REASON,
    STATE_ADDING_DEBT_AMOUNT,
    STATE_EDITING_CHOOSE_DEBT,  # Unused, can be removed
    STATE_EDITING_CHOOSE_WHAT_TO_EDIT,
    STATE_EDITING_AMOUNT,
    STATE_EDITING_REASON,
    STATE_CONFIRMING_CLOSE_DEBT,
    STATE_SUBTRACTING_FROM_DEBT,
    STATE_CONFIRMING_DELETE_DEBTOR,
    STATE_SETTING_PAYMENT_DATE,
    STATE_SETTING_PAYMENT_AMOUNT,
    STATE_EDITING_PAYMENT_DATE,
    STATE_EDITING_PAYMENT_AMOUNT,
) = range(15)


# --- Global Variables ---
DB_NAME = "debt_tracker.db"
user_states = {}  # Track user's current state
current_debtors = {}  # Store debtor info, keyed by chat_id
selected_debts = {}  # Store debt info, keyed by chat_id


# --- Helper Functions ---
async def send_with_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup = None,
):
    """Sends a message with an optional inline keyboard."""
    if update.message:
        await update.message.reply_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )


async def send_simple_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
):
    """Sends a plain text message."""
    await send_with_keyboard(update, context, text)


async def edit_message_with_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup = None,
):
    """Edits an existing message, adding or updating an inline keyboard."""
    if update.callback_query:  # Check if it's a callback query
        await context.bot.edit_message_text(
            text=text,
            chat_id=update.callback_query.message.chat_id,
            message_id=update.callback_query.message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    else:  # If not a callback query, send a new message
       await send_with_keyboard(update, context, text, keyboard)



def clear_user_state(chat_id: int):
    """Clears the conversation state for a given chat ID."""
    user_states.pop(chat_id, None)
    # current_debtors.pop(chat_id, None) # Keep current debtor
    selected_debts.pop(chat_id, None)


# --- Database Initialization ---
def init_db():
    """Initializes the database."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS debtors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                payment_date DATETIME,
                payment_amount REAL,
                UNIQUE(name, chat_id)
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debtor_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                FOREIGN KEY (debtor_id) REFERENCES debtors (id) ON DELETE CASCADE
            )
        """
        )
        conn.commit()


# --- Database Interaction Functions ---
def add_debtor(debtor_name: str, chat_id: int) -> tuple[dict, bool]:
    """Adds a new debtor or retrieves existing."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO debtors (name, chat_id) VALUES (?, ?)",
                (debtor_name, chat_id),
            )
            debtor_id = cursor.lastrowid
            conn.commit()
            return (
                {
                    "id": debtor_id,
                    "name": debtor_name,
                    "chat_id": chat_id,
                    "payment_date": None,
                    "payment_amount": None,
                },
                True,
            )  # Return True for new debtor
        except sqlite3.IntegrityError:
            cursor.execute(
                "SELECT id, name, chat_id, payment_date, payment_amount FROM debtors WHERE name = ? AND chat_id = ?",
                (debtor_name, chat_id),
            )
            row = cursor.fetchone()
            debtor = {
                "id": row[0],
                "name": row[1],
                "chat_id": row[2],
                "payment_date": row[3],
                "payment_amount": row[4],
            }
            return debtor, False  # Return False for existing debtor


def get_debtor_by_name(name: str, chat_id: int) -> dict | None:
    """Retrieves a debtor by name and chat ID."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, chat_id, payment_date, payment_amount FROM debtors WHERE name = ? AND chat_id = ?",
            (name, chat_id),
        )
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "chat_id": row[2],
                "payment_date": row[3],
                "payment_amount": row[4],
            }
        return None


def get_debtor_by_id(debtor_id: int) -> dict | None:
    """Retrieves a debtor by their ID."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, chat_id, payment_date, payment_amount FROM debtors WHERE id = ?",
            (debtor_id,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "chat_id": row[2],
                "payment_date": row[3],
                "payment_amount": row[4],
            }
        return None


def add_debt(debtor_id: int, amount: float, reason: str):
    """Adds a debt for a given debtor."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO debts (debtor_id, amount, reason) VALUES (?, ?, ?)",
            (debtor_id, amount, reason),
        )
        conn.commit()


def list_debtors(chat_id: int) -> list[dict]:
    """Lists all debtors for a given chat ID."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, payment_date, payment_amount FROM debtors WHERE chat_id = ?",
            (chat_id,),
        )
        debtors = []
        for row in cursor.fetchall():
            debtors.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "payment_date": row[2],
                    "payment_amount": row[3],
                }
            )
        return debtors


def list_debts(debtor_id: int) -> list[dict]:
    """Lists all debts for a given debtor ID."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, amount, reason FROM debts WHERE debtor_id = ?", (debtor_id,)
        )
        debts = []
        for row in cursor.fetchall():
            debts.append({"id": row[0], "amount": row[1], "reason": row[2]})
        return debts


def get_debt_by_id(debt_id: int) -> dict | None:
    """Retrieves a debt by its ID."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, debtor_id, amount, reason FROM debts WHERE id = ?", (debt_id,)
        )
        row = cursor.fetchone()
        if row:
            return {"id": row[0], "debtor_id": row[1], "amount": row[2], "reason": row[3]}
        return None


def update_debt_amount(debt_id: int, new_amount: float):
    """Updates the amount of a debt."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debts SET amount = ? WHERE id = ?", (new_amount, debt_id)
        )
        conn.commit()


def update_debt_reason(debt_id: int, new_reason: str):
    """Updates the reason of a debt."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE debts SET reason = ? WHERE id = ?", (new_reason, debt_id))
        conn.commit()


def close_debt(debt_id: int):
    """Deletes a debt (closes it)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
        conn.commit()


def delete_debtor(debtor_id: int):
    """Deletes a debtor and all their associated debts."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM debtors WHERE id = ?", (debtor_id,))
        conn.commit()


def update_debtor_payment_date(debtor_id: int, payment_date: datetime):
    """Updates the payment date for a debtor."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debtors SET payment_date = ? WHERE id = ?", (payment_date, debtor_id)
        )
        conn.commit()


def update_debtor_payment_amount(debtor_id: int, payment_amount: float):
    """Updates the payment amount for a debtor."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debtors SET payment_amount = ? WHERE id = ?",
            (payment_amount, debtor_id),
        )
        conn.commit()


def clear_debtor_payment_date(debtor_id: int):
    """Clears the payment date for a debtor (sets it to NULL)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debtors SET payment_date = NULL WHERE id = ?", (debtor_id,)
        )
        conn.commit()


def clear_debtor_payment_amount(debtor_id: int):
    """Clears the payment amount for a debtor (sets it to NULL)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debtors SET payment_amount = NULL WHERE id = ?", (debtor_id,)
        )
        conn.commit()


# --- CSV Export ---
def generate_csv(chat_id: int) -> str | None:
    """Generates a CSV file with debt information."""
    debtors = list_debtors(chat_id)
    if not debtors:
        return None

    with tempfile.NamedTemporaryFile(
        mode="w+", delete=False, suffix=".csv", encoding="utf-8"
    ) as tmpfile:
        writer = csv.writer(tmpfile)
        header = [
            "Debtor Name",
            "Total Debt",
            "Payment Date",
            "Payment Amount",
            "Debt Reason",
            "Debt Amount",
        ]
        writer.writerow(header)

        for debtor in debtors:
            debts = list_debts(debtor["id"])
            total_debt = sum(debt["amount"] for debt in debts)
            # Format dates and amounts for CSV
            payment_date_str = (
                datetime.strptime(str(debtor["payment_date"]), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
                if debtor["payment_date"]
                else ""
            )  # Correct date conversion
            payment_amount_str = (
                f"{debtor['payment_amount']:.2f}" if debtor["payment_amount"] else ""
            )

            if debts:
                for debt in debts:
                    writer.writerow(
                        [
                            debtor["name"],
                            f"{total_debt:.2f}",
                            payment_date_str,
                            payment_amount_str,
                            debt["reason"],
                            f"{debt['amount']:.2f}",
                        ]
                    )
            else:
                writer.writerow(
                    [
                        debtor["name"],
                        f"{total_debt:.2f}",
                        payment_date_str,
                        payment_amount_str,
                        "",
                        "0.00",
                    ]
                )
        return tmpfile.name


# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    clear_user_state(update.message.chat_id)
    try:
        with open("botBanner.jpeg", "rb") as f:
            await update.message.reply_photo(photo=f)
    except FileNotFoundError:
        await update.message.reply_text(
            "Привет! Я бот DebtTracker. Я помогу тебе вести учет долгов."
        )

    await send_simple_message(
        update,
        context,
        "Привет! Я бот DebtTracker. Я помогу тебе вести учет долгов.\n\n"
        "Основные команды:\n"
        "/add - Добавить долг\n"
        "/debts - Посмотреть список должников и долги\n"
        "/exportcsv - Выгрузить данные в CSV\n"
        "/help - Помощь и список команд",
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /add command."""
    clear_user_state(update.message.chat_id)
    user_states[update.message.chat_id] = STATE_ADDING_DEBTOR_NAME
    await send_simple_message(update, context, "Введи имя должника:")


async def debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /debts command."""
    clear_user_state(update.message.chat_id)
    chat_id = update.effective_chat.id
    debtors = list_debtors(chat_id)

    if not debtors:
        await send_simple_message(
            update, context, "У тебя пока нет должников. Используй /add, чтобы добавить."
        )
        return

    keyboard_buttons = []
    for debtor in debtors:
        debts_count = len(list_debts(debtor["id"]))
        if debts_count % 10 == 1 and debts_count % 100 != 11:
            debt_plural = "долг"
        elif (
            debts_count % 10 >= 2
            and debts_count % 10 <= 4
            and not (debts_count % 100 >= 12 and debts_count % 100 <= 14)
        ):
            debt_plural = "долга"
        else:
            debt_plural = "долгов"

        button_text = f"{debtor['name']} ({debts_count} {debt_plural})"
        callback_data = f"select_debtor:{debtor['id']}"
        keyboard_buttons.append(
            [InlineKeyboardButton(button_text, callback_data=callback_data)]
        )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    await send_with_keyboard(update, context, "*Твои должники:*", keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /help command."""
    clear_user_state(update.message.chat_id)
    text = (
        "**Команды бота DebtTracker:**\n\n"
        "/add - Добавить новый долг. Бот спросит имя должника, причину и сумму.\n"
        "/debts - Показать список всех твоих должников. Можно выбрать должника, "
        "чтобы увидеть детализацию долгов, закрыть или отредактировать долги.\n"
        "/exportcsv - Выгрузить данные в CSV файл.\n"
        "/help - Показать это сообщение со списком команд."
    )
    await send_simple_message(update, context, text)


async def exportcsv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /exportcsv command."""
    clear_user_state(update.message.chat_id)
    chat_id = update.effective_chat.id
    file_path = generate_csv(chat_id)

    if not file_path:
        await send_simple_message(
            update, context, "Нет данных для выгрузки. Сначала добавьте должников."
        )
        return

    try:
        with open(file_path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f)
    except Exception as e:
        logger.error(f"Error sending CSV: {e}")
        await send_simple_message(update, context, "Произошла ошибка при отправке файла.")
    finally:
        if os.path.exists(file_path):  # Check if file still exists
            os.remove(file_path)


# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages based on user's current state."""
    chat_id = update.message.chat_id
    text = update.message.text
    state = user_states.get(chat_id, STATE_IDLE)

    if state == STATE_ADDING_DEBTOR_NAME:
        debtor, is_new = add_debtor(text, chat_id)
        if not is_new:
            await send_simple_message(
                update,
                context,
                f"Должник с именем *{text}* уже существует. Пожалуйста, введите другое имя.",
            )
            return

        current_debtors[chat_id] = debtor  # Store *after* successful add
        user_states[chat_id] = STATE_ADDING_DEBT_REASON
        await send_simple_message(
            update, context, f"Какова причина долга для *{debtor['name']}*?"
        )

    elif state == STATE_ADDING_DEBT_REASON:
        selected_debts[chat_id] = {
            "debtor_id": current_debtors[chat_id]["id"],
            "reason": text,
        }
        user_states[chat_id] = STATE_ADDING_DEBT_AMOUNT
        await send_simple_message(
            update,
            context,
            f"Сколько *{current_debtors[chat_id]['name']}* должен за *{text}*?",
        )

    elif state == STATE_ADDING_DEBT_AMOUNT:
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму (положительное число)."
            )
            return

        debt = {
            "debtor_id": current_debtors[chat_id]["id"],
            "amount": amount,
            "reason": selected_debts[chat_id]["reason"],
        }
        add_debt(**debt)  # Add the debt to the database

        await send_simple_message(
            update,
            context,
            f"✅ Долг добавлен! *{current_debtors[chat_id]['name']}* должен *{amount:.2f} ₽* за *{debt['reason']}*.",
        )
        clear_user_state(chat_id)

    elif state == STATE_EDITING_AMOUNT:
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму (положительное число)."
            )
            return

        update_debt_amount(selected_debts[chat_id]["id"], amount)
        await send_simple_message(update, context, "Сумма долга обновлена.")
        await show_debtor_details(
            update, context, current_debtors[chat_id]["id"]
        )  # Refresh details
        clear_user_state(chat_id)

    elif state == STATE_EDITING_REASON:
        update_debt_reason(selected_debts[chat_id]["id"], text)
        await send_simple_message(update, context, "Причина долга обновлена.")
        await show_debtor_details(
            update, context, current_debtors[chat_id]["id"]
        )  # Refresh details
        clear_user_state(chat_id)

    elif state == STATE_SUBTRACTING_FROM_DEBT:
        try:
            amount_to_subtract = float(text)
            if amount_to_subtract <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму (положительное число)."
            )
            return

        debt = selected_debts[chat_id]
        if amount_to_subtract > debt["amount"]:
            await send_simple_message(
                update, context, "Сумма вычитания не может быть больше суммы долга."
            )
            return

        new_amount = debt["amount"] - amount_to_subtract
        update_debt_amount(debt["id"], new_amount)
        if new_amount == 0:
            close_debt(debt["id"])
            await send_simple_message(
                update,
                context,
                f"✅ Долг *{debt['amount']:.2f} ₽* за *{debt['reason']}* погашен и закрыт.",
            )
        else:
            await send_simple_message(
                update,
                context,
                f"Вычтено *{amount_to_subtract:.2f} ₽*. Остаток долга: *{new_amount:.2f} ₽*.",
            )
        await show_debtor_details(
            update, context, debt["debtor_id"]
        )  # Refresh details
        clear_user_state(chat_id)

    elif state == STATE_SETTING_PAYMENT_DATE:
        try:
            date_formats = ["%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d-%m-%y"]
            payment_date = None
            for fmt in date_formats:
                try:
                    payment_date = datetime.strptime(text, fmt)
                    break  # Exit loop if date is parsed successfully
                except ValueError:
                    continue  # Try next format

            if payment_date is None:
                raise ValueError("Invalid date format")

        except ValueError:
            await send_simple_message(
                update,
                context,
                "Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ или ДД.ММ.ГГ.",
            )
            return

        debtor_id = current_debtors[chat_id]["id"]
        update_debtor_payment_date(debtor_id, payment_date)
        await send_simple_message(
            update,
            context,
            f"Дата платежа для *{current_debtors[chat_id]['name']}* установлена на *{payment_date.strftime('%d.%m.%Y')}*.",
        )
        await show_debtor_details(update, context, debtor_id)
        clear_user_state(chat_id)

    elif state == STATE_SETTING_PAYMENT_AMOUNT:
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму платежа (положительное число)."
            )
            return

        debtor_id = current_debtors[chat_id]["id"]
        update_debtor_payment_amount(debtor_id, amount)
        await send_simple_message(
            update,
            context,
            f"Сумма платежа для *{current_debtors[chat_id]['name']}* установлена на *{amount:.2f} ₽*.",
        )
        await show_debtor_details(update, context, debtor_id)
        clear_user_state(chat_id)

    elif state == STATE_EDITING_PAYMENT_DATE:
        try:
            date_formats = ["%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d-%m-%y"]
            payment_date = None
            for fmt in date_formats:
                try:
                    payment_date = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if payment_date is None:
                raise ValueError("Invalid date format")
        except ValueError:
            await send_simple_message(
                update,
                context,
                "Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ или ДД.ММ.ГГ.",
            )
            return

        debtor_id = current_debtors[chat_id]["id"]
        update_debtor_payment_date(debtor_id, payment_date)
        await send_simple_message(
            update,
            context,
            f"Дата платежа для *{current_debtors[chat_id]['name']}* обновлена на *{payment_date.strftime('%d.%m.%Y')}*.",
        )
        await show_debtor_details(update, context, debtor_id)
        clear_user_state(chat_id)

    elif state == STATE_EDITING_PAYMENT_AMOUNT:
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму платежа (положительное число)."
            )
            return

        debtor_id = current_debtors[chat_id]["id"]
        update_debtor_payment_amount(debtor_id, amount)
        await send_simple_message(
            update, context, "Сумма платежа обновлена."
        )  # Simplified message
        await show_debtor_details(update, context, debtor_id)
        clear_user_state(chat_id)

    else:
        await send_simple_message(
            update,
            context,
            "Используй /add для добавления долга, /debts для просмотра долгов.",
        )
        clear_user_state(chat_id)


# --- Callback Query Handler ---

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # Always answer callback queries!
    data = query.data
    chat_id = query.message.chat_id

    if data.startswith("select_debtor:"):
        debtor_id = int(data.split(":")[1])
        debtor = get_debtor_by_id(debtor_id)
        if not debtor:
            await context.bot.send_message(chat_id=chat_id, text="Должник не найден.")
            clear_user_state(chat_id)
            return

        current_debtors[chat_id] = debtor  # Store *before* any clearing
        clear_user_state(chat_id)  # Keep current debtor
        await show_debtor_details(update, context, debtor_id)

    elif data.startswith("close_debt:"):
        debt_id = int(data.split(":")[1])
        debt = get_debt_by_id(debt_id)
        if not debt:
            await context.bot.send_message(
                chat_id=chat_id, text="Долг не найден."
            )  # Inform user
            return  # Exit if debt not found

        selected_debts[chat_id] = debt
        user_states[chat_id] = STATE_CONFIRMING_CLOSE_DEBT

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да, закрыть", callback_data=f"confirm_close:{debt_id}"),
                    InlineKeyboardButton("❌ Отмена", callback_data="cancel_operation"),
                ]
            ]
        )
        await edit_message_with_keyboard(
            update,
            context,
            f"Вы уверены, что хотите закрыть долг *{debt['amount']:.2f} ₽* за *{debt['reason']}*?",
            keyboard,
        )

    elif data.startswith("confirm_close:"):
        debt_id = int(data.split(":")[1])
        close_debt(debt_id)  # Close the debt
        await edit_message_with_keyboard(update, context, "Долг закрыт.")
        if chat_id in current_debtors:
           await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        clear_user_state(chat_id)


    elif data == "cancel_operation":
        await edit_message_with_keyboard(update, context, "Операция отменена.")
        if chat_id in current_debtors: # Check if the key exists
          await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        clear_user_state(chat_id)

    elif data.startswith("edit_debt:"):
        debt_id = int(data.split(":")[1])
        debt = get_debt_by_id(debt_id)
        if not debt:
            await context.bot.send_message(chat_id=chat_id, text="Долг не найден.")
            return

        selected_debts[chat_id] = debt  # Store *before* switching state
        user_states[chat_id] = STATE_EDITING_CHOOSE_WHAT_TO_EDIT
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Изменить сумму", callback_data=f"edit_amount:{debt_id}"),
                    InlineKeyboardButton("Изменить причину", callback_data=f"edit_reason:{debt_id}"),
                ],
                [
                    InlineKeyboardButton(
                        "Вычесть из долга", callback_data=f"subtract_from_debt:{debt_id}"
                    )
                ],
            ]
        )
        await edit_message_with_keyboard(
            update, context, "Что вы хотите изменить?", keyboard
        )

    elif data.startswith("edit_amount:"):
        debt_id = int(data.split(":")[1])
        selected_debts[chat_id] = {"id": debt_id}  # Store only debt ID
        user_states[chat_id] = STATE_EDITING_AMOUNT
        await edit_message_with_keyboard(update, context, "Введите новую сумму:")

    elif data.startswith("edit_reason:"):
        debt_id = int(data.split(":")[1])
        selected_debts[chat_id] = {"id": debt_id}  # Store only debt ID
        user_states[chat_id] = STATE_EDITING_REASON
        await edit_message_with_keyboard(update, context, "Введите новую причину:")

    elif data.startswith("subtract_from_debt:"):
        debt_id = int(data.split(":")[1])
        debt = get_debt_by_id(debt_id)
        if not debt:
            await context.bot.send_message(chat_id=chat_id, text="Долг не найден.")
            return
        selected_debts[chat_id] = debt  # Store the debt info
        user_states[chat_id] = STATE_SUBTRACTING_FROM_DEBT
        await edit_message_with_keyboard(
            update, context, f"Какую сумму вычесть из долга *{debt['amount']:.2f} ₽*?"
        )
    elif data == "add_debt_to_existing":
        # Don't clear, set to adding reason state
        user_states[chat_id] = STATE_ADDING_DEBT_REASON
        await edit_message_with_keyboard(
            update,
            context,
            f"Какова причина долга для *{current_debtors[chat_id]['name']}*?",
        )

    elif data == "delete_debtor":
        # Don't clear state here!  We need the debtor info.
        user_states[chat_id] = STATE_CONFIRMING_DELETE_DEBTOR
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да, удалить", callback_data="confirm_delete_debtor"),
                    InlineKeyboardButton("❌ Отмена", callback_data="cancel_operation"),
                ]
            ]
        )
        await edit_message_with_keyboard(
            update,
            context,
            f"Вы уверены, что хотите удалить должника *{current_debtors[chat_id]['name']}*? *Все долги этого должника будут удалены!*",
            keyboard,
        )

    elif data == "confirm_delete_debtor":
        debtor_id = current_debtors[chat_id]["id"]
        delete_debtor(debtor_id)  # Delete from the database
        await edit_message_with_keyboard(
            update, context, f"Должник *{current_debtors[chat_id]['name']}* и все долги удалены."
        )
        current_debtors.pop(chat_id, None)  # *Now* clear the debtor
        clear_user_state(chat_id)

    elif data == "set_payment_date":
        user_states[chat_id] = STATE_SETTING_PAYMENT_DATE
        await edit_message_with_keyboard(
            update, context, "Введите дату платежа (ДД.ММ.ГГГГ или ДД.ММ.ГГ):"
        )

    elif data == "set_payment_amount":
        user_states[chat_id] = STATE_SETTING_PAYMENT_AMOUNT
        await edit_message_with_keyboard(update, context, "Введите сумму платежа:")

    elif data == "clear_payment_date":
        if chat_id in current_debtors:
            clear_debtor_payment_date(current_debtors[chat_id]["id"])
            await edit_message_with_keyboard(update, context, "Дата платежа очищена.")
            await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        else:
             await context.bot.send_message(chat_id=chat_id, text="Ошибка: нет текущего должника.")

        clear_user_state(chat_id)

    elif data == "clear_payment_amount":
        if chat_id in current_debtors:
            clear_debtor_payment_amount(current_debtors[chat_id]["id"])
            await edit_message_with_keyboard(update, context, "Сумма платежа очищена.")
            await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        else:
            await context.bot.send_message(chat_id=chat_id, text="Ошибка: нет текущего должника.")

        clear_user_state(chat_id)

    elif data == "edit_payment_date":
        user_states[chat_id] = STATE_EDITING_PAYMENT_DATE
        await edit_message_with_keyboard(
            update, context, "Введите новую дату платежа (ДД.ММ.ГГГГ или ДД.ММ.ГГ):"
        )

    elif data == "edit_payment_amount":
        user_states[chat_id] = STATE_EDITING_PAYMENT_AMOUNT
        await edit_message_with_keyboard(update, context, "Введите новую сумму платежа:")


# --- Show Debtor Details ---
async def show_debtor_details(
    update: Update, context: ContextTypes.DEFAULT_TYPE, debtor_id: int
):
    """Displays detailed information about a debtor and their debts."""
    debtor = get_debtor_by_id(debtor_id)
    if not debtor:
        if update.callback_query:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text="Должник не найден."
            )  # Use effective_chat
        else:
            await update.message.reply_text("Должник не найден.")  # Use reply_text
        return

    chat_id = (
        update.effective_chat.id
    )  # Get chat_id *before* any state clearing
    current_debtors[chat_id] = debtor  # *Always* store debtor

    debts = list_debts(debtor_id)
    total_debt = sum(debt["amount"] for debt in debts)

    debts_text = f"*Долги {debtor['name']}:*\n\n"
    keyboard_buttons = []

    for debt in debts:
        debts_text += f"- *{debt['amount']:.2f} ₽* за *{debt['reason']}*\n"
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "✏️ Редактировать", callback_data=f"edit_debt:{debt['id']}"
                ),
                InlineKeyboardButton(
                    "✅ Закрыть", callback_data=f"close_debt:{debt['id']}"
                ),
            ]
        )

    debts_text += f"\n*Общая сумма долга: {total_debt:.2f} ₽*"

    if debtor.get("payment_date"):
        debts_text += f"\n\n*Дата платежа:* {datetime.strptime(str(debtor['payment_date']), '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y')}"
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "Изменить дату", callback_data="edit_payment_date"
                ),
                InlineKeyboardButton(
                    "Очистить дату", callback_data="clear_payment_date"
                ),
            ]
        )
    else:
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "Указать дату платежа", callback_data="set_payment_date"
                ),
            ]
        )

    if debtor.get("payment_amount"):
        debts_text += f"\n*Сумма платежа:* {debtor['payment_amount']:.2f} ₽"
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "Изменить сумму", callback_data="edit_payment_amount"
                ),
                InlineKeyboardButton(
                    "Очистить сумму", callback_data="clear_payment_amount"
                ),
            ]
        )
    else:
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "Указать сумму платежа", callback_data="set_payment_amount"
                ),
            ]
        )

    keyboard_buttons.append(
        [
            InlineKeyboardButton("➕ Добавить долг", callback_data="add_debt_to_existing"),
            InlineKeyboardButton("🗑️ Удалить должника", callback_data="delete_debtor"),
        ]
    )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    if update.callback_query:
        await edit_message_with_keyboard(update, context, debts_text, keyboard)
    else:
        await send_with_keyboard(update, context, debts_text, keyboard)



def main():
    """Main function to run the bot."""
    load_dotenv()
    bot_token = os.getenv("TELEGRAM_API_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables.")
        return

    init_db()  # Initialize the database

    # Use ApplicationBuilder for a more modern approach
    app = ApplicationBuilder().token(bot_token).build()

    # --- Register Command Handlers ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("debts", debts))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("exportcsv", exportcsv))

    # --- Register Message Handler (for text input) ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- Register Callback Query Handler (for inline buttons) ---
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # --- Start the Bot ---
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()