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

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы, определяющие состояния диалога с пользователем.  Каждое состояние
# соответствует определенному этапу ввода данных или выбора действия.
(
    STATE_IDLE,  # Состояние ожидания (начальное состояние)
    STATE_ADDING_DEBTOR_NAME,  # Ожидание ввода имени должника
    STATE_ADDING_DEBT_REASON,  # Ожидание ввода причины долга
    STATE_ADDING_DEBT_AMOUNT,  # Ожидание ввода суммы долга
    STATE_EDITING_CHOOSE_DEBT,  # Не используется, можно удалить
    STATE_EDITING_CHOOSE_WHAT_TO_EDIT,  # Ожидание выбора, что редактировать (сумму или причину)
    STATE_EDITING_AMOUNT,  # Ожидание ввода новой суммы долга
    STATE_EDITING_REASON,  # Ожидание ввода новой причины долга
    STATE_CONFIRMING_CLOSE_DEBT,  # Ожидание подтверждения закрытия долга
    STATE_SUBTRACTING_FROM_DEBT,  # Ожидание ввода суммы для частичного погашения долга
    STATE_CONFIRMING_DELETE_DEBTOR,  # Ожидание подтверждения удаления должника
    STATE_SETTING_PAYMENT_DATE,  # Ожидание ввода даты платежа
    STATE_SETTING_PAYMENT_AMOUNT,  # Ожидание ввода суммы платежа
    STATE_EDITING_PAYMENT_DATE,  # Ожидание редактирования даты
    STATE_EDITING_PAYMENT_AMOUNT,  # Ожидание редактирвоания суммы
) = range(15)


# Глобальные переменные
DB_NAME = "debt_tracker.db"  # Имя файла базы данных
user_states = {}  # Словарь для отслеживания состояния каждого пользователя (chat_id: состояние)
current_debtors = {}  # Словарь для хранения информации о текущем должнике (chat_id: информация о должнике)
selected_debts = {}  # Словарь для хранения информации о выбранном долге (chat_id: информация о долге)


# Вспомогательные функции (улучшенный UX)

async def send_with_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, keyboard: InlineKeyboardMarkup = None):
    """Отправляет сообщение с инлайн-клавиатурой (кнопками)."""
    # Проверяем, откуда пришло обновление (из сообщения или из callback query)
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def send_simple_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Отправляет простое текстовое сообщение (без клавиатуры)."""
    await send_with_keyboard(update, context, text)  # Используем общую функцию для единообразия


async def edit_message_with_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, keyboard: InlineKeyboardMarkup = None):
    """Редактирует существующее сообщение, добавляя или обновляя инлайн-клавиатуру.
       Если это callback query, редактируем сообщение.  Иначе отправляем новое.
    """
    if update.callback_query:  # Проверяем, является ли обновление callback query
        await context.bot.edit_message_text(
            text=text,
            chat_id=update.callback_query.message.chat_id,
            message_id=update.callback_query.message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    else:  # Если это не callback query (например, команда), отправляем новое сообщение
        await send_with_keyboard(update, context, text, keyboard)


def clear_user_state(chat_id: int):
    """Сбрасывает состояние пользователя и очищает связанные данные."""
    user_states.pop(chat_id, None)  # Удаляем запись о состоянии пользователя, если она есть
    # current_debtors.pop(chat_id, None) # Оставляем current_debtor, чтобы можно было добавлять долги к существующему
    selected_debts.pop(chat_id, None)  # Удаляем запись о выбранном долге, если она есть


# Инициализация базы данных
def init_db():
    """Создает таблицы в базе данных, если они не существуют."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Таблица должников: ID, имя, chat_id пользователя, дата платежа, сумма платежа
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS debtors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                payment_date DATETIME,
                payment_amount REAL,
                UNIQUE(name, chat_id)  -- Гарантирует уникальность пары (имя, chat_id)
            )
        """
        )
        # Таблица долгов: ID, ID должника, сумма долга, причина долга
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debtor_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                FOREIGN KEY (debtor_id) REFERENCES debtors (id) ON DELETE CASCADE  -- При удалении должника удаляются и его долги
            )
        """
        )
        conn.commit()


# Функции для взаимодействия с базой данных (CRUD - Create, Read, Update, Delete)
def add_debtor(debtor_name: str, chat_id: int) -> tuple[dict, bool]:
    """Добавляет нового должника в базу данных или возвращает существующего.

    Args:
        debtor_name: Имя должника.
        chat_id: ID чата пользователя.

    Returns:
        Кортеж: (информация о должнике, флаг нового должника).
        Флаг нового должника True, если должник был добавлен, False, если уже существовал.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        try:
            # Пытаемся добавить нового должника
            cursor.execute(
                "INSERT INTO debtors (name, chat_id) VALUES (?, ?)",
                (debtor_name, chat_id),
            )
            debtor_id = cursor.lastrowid  # Получаем ID добавленного должника
            conn.commit()
            # Возвращаем информацию о должнике и флаг True (новый должник)
            return (
                {
                    "id": debtor_id,
                    "name": debtor_name,
                    "chat_id": chat_id,
                    "payment_date": None,  # Дату и сумму платежа устанавливаем позже
                    "payment_amount": None,
                },
                True,
            )
        except sqlite3.IntegrityError:
            # Если должник с таким именем уже существует, получаем его данные
            cursor.execute(
                "SELECT id, name, chat_id, payment_date, payment_amount FROM debtors WHERE name = ? AND chat_id = ?",
                (debtor_name, chat_id),
            )
            row = cursor.fetchone()  # Получаем строку с данными должника
            # Преобразуем строку в словарь
            debtor = {
                "id": row[0],
                "name": row[1],
                "chat_id": row[2],
                "payment_date": row[3],
                "payment_amount": row[4],
            }
            # Возвращаем информацию о должнике и флаг False (должник уже существовал)
            return debtor, False


def get_debtor_by_name(name: str, chat_id: int) -> dict | None:
    """Возвращает информацию о должнике по имени и chat_id.

    Args:
        name: Имя должника.
        chat_id: ID чата пользователя.

    Returns:
        Словарь с информацией о должнике или None, если должник не найден.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, chat_id, payment_date, payment_amount FROM debtors WHERE name = ? AND chat_id = ?",
            (name, chat_id),
        )
        row = cursor.fetchone()  # Получаем строку с данными должника (или None)
        if row:
            # Преобразуем строку в словарь
            return {
                "id": row[0],
                "name": row[1],
                "chat_id": row[2],
                "payment_date": row[3],
                "payment_amount": row[4],
            }
        return None  # Возвращаем None, если должник не найден


def get_debtor_by_id(debtor_id: int) -> dict | None:
    """Возвращает информацию о должнике по ID.

    Args:
        debtor_id: ID должника.

    Returns:
        Словарь с информацией о должнике или None, если должник не найден.
    """
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
    """Добавляет запись о долге для указанного должника.

    Args:
        debtor_id: ID должника.
        amount: Сумма долга.
        reason: Причина долга.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO debts (debtor_id, amount, reason) VALUES (?, ?, ?)",
            (debtor_id, amount, reason),
        )
        conn.commit()


def list_debtors(chat_id: int) -> list[dict]:
    """Возвращает список всех должников для указанного пользователя (chat_id).

    Args:
        chat_id: ID чата пользователя.

    Returns:
        Список словарей с информацией о должниках.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, payment_date, payment_amount FROM debtors WHERE chat_id = ?",
            (chat_id,),
        )
        debtors = []
        for row in cursor.fetchall():  # Перебираем все строки результата запроса
            # Преобразуем каждую строку в словарь и добавляем в список
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
    """Возвращает список всех долгов для указанного должника.

    Args:
        debtor_id: ID должника.

    Returns:
        Список словарей с информацией о долгах.
    """
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
    """Возвращает информацию о долге по ID.

    Args:
        debt_id: ID долга.

    Returns:
        Словарь с информацией о долге или None, если долг не найден.
    """
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
    """Обновляет сумму долга.

    Args:
        debt_id: ID долга.
        new_amount: Новая сумма долга.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debts SET amount = ? WHERE id = ?", (new_amount, debt_id)
        )
        conn.commit()


def update_debt_reason(debt_id: int, new_reason: str):
    """Обновляет причину долга.

    Args:
        debt_id: ID долга.
        new_reason: Новая причина долга.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE debts SET reason = ? WHERE id = ?", (new_reason, debt_id))
        conn.commit()


def close_debt(debt_id: int):
    """Закрывает (удаляет) долг.

    Args:
        debt_id: ID долга.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
        conn.commit()


def delete_debtor(debtor_id: int):
    """Удаляет должника и все связанные с ним долги.

    Args:
        debtor_id: ID должника.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Удаляем должника (связанные долги удалятся автоматически благодаря ON DELETE CASCADE)
        cursor.execute("DELETE FROM debtors WHERE id = ?", (debtor_id,))
        conn.commit()


def update_debtor_payment_date(debtor_id: int, payment_date: datetime):
    """Обновляет дату платежа для должника.

    Args:
        debtor_id:  ID должника.
        payment_date:  Дата платежа.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE debtors SET payment_date = ? WHERE id = ?", (payment_date, debtor_id))
        conn.commit()

def update_debtor_payment_amount(debtor_id: int, payment_amount: float):
    """
    Обновляет сумму платежа
    Args:
        debtor_id: ID должника.
        payment_amount: Сумма платежа.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE debtors SET payment_amount = ? WHERE id = ?", (payment_amount, debtor_id))
        conn.commit()

def clear_debtor_payment_date(debtor_id: int):
    """Очищает (устанавливает в NULL) дату платежа для должника.

    Args:
        debtor_id: ID должника.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debtors SET payment_date = NULL WHERE id = ?", (debtor_id,)
        )
        conn.commit()


def clear_debtor_payment_amount(debtor_id: int):
    """Очищает (устанавливает в NULL) сумму платежа для должника.

    Args:
        debtor_id: ID должника.
    """
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE debtors SET payment_amount = NULL WHERE id = ?", (debtor_id,)
        )
        conn.commit()



# Генерация CSV файла (экспорт данных)
def generate_csv(chat_id: int) -> str | None:
    """Генерирует CSV файл с информацией о долгах пользователя.

    Args:
        chat_id: ID чата пользователя.

    Returns:
        Путь к временному CSV файлу или None, если нет данных для экспорта.
    """
    debtors = list_debtors(chat_id)  # Получаем список должников пользователя
    if not debtors:
        return None  # Если должников нет, возвращаем None

    # Создаем временный файл для записи CSV данных
    with tempfile.NamedTemporaryFile(
        mode="w+", delete=False, suffix=".csv", encoding="utf-8"
    ) as tmpfile:
        writer = csv.writer(tmpfile)
        # Заголовок CSV файла
        header = [
            "Имя должника",
            "Общий долг",
            "Дата платежа",
            "Сумма платежа",
            "Причина долга",
            "Сумма долга",
        ]
        writer.writerow(header)  # Записываем заголовок в файл

        for debtor in debtors:
            debts = list_debts(debtor["id"])  # Получаем список долгов для каждого должника
            total_debt = sum(debt["amount"] for debt in debts)  # Считаем общую сумму долга

            # Форматируем дату и сумму платежа для записи в CSV
            payment_date_str = (
                datetime.strptime(str(debtor["payment_date"]), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
                if debtor["payment_date"]
                else ""  # Если дата платежа не установлена, записываем пустую строку
            )
            payment_amount_str = (
                f"{debtor['payment_amount']:.2f}" if debtor["payment_amount"] else ""
            )

            if debts:
                # Если есть долги, записываем информацию о каждом долге
                for debt in debts:
                    writer.writerow(
                        [
                            debtor["name"],
                            f"{total_debt:.2f}",  # Общая сумма долга
                            payment_date_str,  # Дата платежа
                            payment_amount_str,  # Сумма платежа
                            debt["reason"],  # Причина долга
                            f"{debt['amount']:.2f}",  # Сумма долга
                        ]
                    )
            else:
                # Если долгов нет, записываем строку с нулевым долгом
                writer.writerow(
                    [
                        debtor["name"],
                        f"{total_debt:.2f}",
                        payment_date_str,
                        payment_amount_str,
                        "",  # Пустая причина долга
                        "0.00",  # Нулевая сумма долга
                    ]
                )
        return tmpfile.name  # Возвращаем имя временного файла


# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start.  Приветствует пользователя и выводит основную информацию."""
    clear_user_state(update.message.chat_id)  # Сбрасываем состояние пользователя
    try:
        # Пробуем отправить приветственную картинку
        with open("botBanner.jpeg", "rb") as f:
            await update.message.reply_photo(photo=f)
    except FileNotFoundError:
        # Если картинки нет, отправляем текстовое приветствие
        await update.message.reply_text(
            "Привет! Я бот DebtTracker. Я помогу тебе вести учет долгов."
        )

    # Отправляем приветственное сообщение и список основных команд
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
    """Обработчик команды /add.  Начинает процесс добавления нового долга."""
    clear_user_state(update.message.chat_id)
    user_states[update.message.chat_id] = STATE_ADDING_DEBTOR_NAME  # Переводим пользователя в состояние ввода имени должника
    await send_simple_message(update, context, "Введи имя должника:")


async def debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /debts.  Выводит список должников пользователя."""
    clear_user_state(update.message.chat_id)
    chat_id = update.effective_chat.id  # Получаем ID чата пользователя
    debtors = list_debtors(chat_id)  # Получаем список должников пользователя

    if not debtors:
        # Если должников нет, сообщаем об этом пользователю
        await send_simple_message(
            update, context, "У тебя пока нет должников. Используй /add, чтобы добавить."
        )
        return

    # Создаем инлайн-клавиатуру со списком должников
    keyboard_buttons = []
    for debtor in debtors:
        debts_count = len(list_debts(debtor["id"]))  # Считаем количество долгов у должника

        #  Делаем читабельный вывод количества долгов.
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

        # Текст кнопки: Имя должника (количество долгов)
        button_text = f"{debtor['name']} ({debts_count} {debt_plural})"
        callback_data = f"select_debtor:{debtor['id']}"  # Данные, которые будут отправлены при нажатии кнопки
        keyboard_buttons.append(
            [InlineKeyboardButton(button_text, callback_data=callback_data)]
        )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)  # Создаем клавиатуру из списка кнопок
    await send_with_keyboard(update, context, "*Твои должники:*", keyboard)  # Отправляем сообщение с клавиатурой


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help.  Выводит справку по командам бота."""
    clear_user_state(update.message.chat_id)
    # Формируем текст справки
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
    """Обработчик команды /exportcsv.  Отправляет пользователю CSV файл с данными о долгах."""
    clear_user_state(update.message.chat_id)
    chat_id = update.effective_chat.id
    file_path = generate_csv(chat_id)  # Генерируем CSV файл

    if not file_path:
        # Если файл не был создан (нет данных), сообщаем об этом пользователю
        await send_simple_message(
            update, context, "Нет данных для выгрузки. Сначала добавьте должников."
        )
        return

    try:
        # Отправляем файл пользователю
        with open(file_path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f)
    except Exception as e:
        # Если произошла ошибка при отправке файла, логируем ее и сообщаем пользователю
        logger.error(f"Error sending CSV: {e}")
        await send_simple_message(update, context, "Произошла ошибка при отправке файла.")
    finally:
        # Удаляем временный файл
        if os.path.exists(file_path):  # Проверяем существует ли файл
            os.remove(file_path)


# Обработчик текстовых сообщений (ввод данных)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения от пользователя в зависимости от текущего состояния."""
    chat_id = update.message.chat_id
    text = update.message.text
    state = user_states.get(chat_id, STATE_IDLE)  # Получаем текущее состояние пользователя, по умолчанию - IDLE

    if state == STATE_ADDING_DEBTOR_NAME:
        # Добавляем должника
        debtor, is_new = add_debtor(text, chat_id)
        if not is_new:
            # Если должник с таким именем уже существует, сообщаем об этом.
            await send_simple_message(
                update,
                context,
                f"Должник с именем *{text}* уже существует. Пожалуйста, введите другое имя.",
            )
            return

        current_debtors[chat_id] = debtor  # Сохраняем информацию о добавленном (или существующем) должнике
        user_states[chat_id] = STATE_ADDING_DEBT_REASON  # Переводим в состояние ввода причины долга
        await send_simple_message(
            update, context, f"Какова причина долга для *{debtor['name']}*?"
        )

    elif state == STATE_ADDING_DEBT_REASON:
        # Сохраняем причину долга
        selected_debts[chat_id] = {
            "debtor_id": current_debtors[chat_id]["id"],
            "reason": text,
        }
        user_states[chat_id] = STATE_ADDING_DEBT_AMOUNT  # Переводим в состояние ввода суммы долга
        await send_simple_message(
            update,
            context,
            f"Сколько *{current_debtors[chat_id]['name']}* должен за *{text}*?",
        )

    elif state == STATE_ADDING_DEBT_AMOUNT:
        # Добавляем долг в БД
        try:
            amount = float(text)  # Преобразуем введенный текст в число
            if amount <= 0:
                raise ValueError  # Если введено неположительное число, вызываем ошибку
        except ValueError:
            # Если не удалось преобразовать в число или число неположительное, сообщаем об ошибке
            await send_simple_message(
                update, context, "Введите корректную сумму (положительное число)."
            )
            return

        # Формируем словарь с информацией о долге
        debt = {
            "debtor_id": current_debtors[chat_id]["id"],
            "amount": amount,
            "reason": selected_debts[chat_id]["reason"],
        }
        add_debt(**debt)  # Добавляем долг в базу данных, используя распаковку словаря

        await send_simple_message(
            update,
            context,
            f"✅ Долг добавлен! *{current_debtors[chat_id]['name']}* должен *{amount:.2f} ₽* за *{debt['reason']}*.",
        )
        clear_user_state(chat_id)  # Сбрасываем состояние

    elif state == STATE_EDITING_AMOUNT:
        # Обновляем сумму долга
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму (положительное число)."
            )
            return

        update_debt_amount(selected_debts[chat_id]["id"], amount)  # Обновляем сумму долга в базе данных
        await send_simple_message(update, context, "Сумма долга обновлена.")
        await show_debtor_details(
            update, context, current_debtors[chat_id]["id"]
        )  # Обновляем отображение деталей должника
        clear_user_state(chat_id)  # Сбрасываем состояние

    elif state == STATE_EDITING_REASON:
        # Обновляем причину долга
        update_debt_reason(selected_debts[chat_id]["id"], text)  # Обновляем причину долга в базе данных
        await send_simple_message(update, context, "Причина долга обновлена.")
        await show_debtor_details(
            update, context, current_debtors[chat_id]["id"]
        )  # Обновляем отображение деталей должника
        clear_user_state(chat_id)

    elif state == STATE_SUBTRACTING_FROM_DEBT:
        # Вычитаем сумму из долга
        try:
            amount_to_subtract = float(text)
            if amount_to_subtract <= 0:
                raise ValueError
        except ValueError:
            await send_simple_message(
                update, context, "Введите корректную сумму (положительное число)."
            )
            return

        debt = selected_debts[chat_id]  # Получаем информацию о выбранном долге
        if amount_to_subtract > debt["amount"]:
            # Если сумма вычитания больше суммы долга, сообщаем об ошибке
            await send_simple_message(
                update, context, "Сумма вычитания не может быть больше суммы долга."
            )
            return

        new_amount = debt["amount"] - amount_to_subtract  # Вычисляем новую сумму долга
        update_debt_amount(debt["id"], new_amount)  # Обновляем сумму долга в базе данных
        if new_amount == 0:
            # Если долг полностью погашен, закрываем его
            close_debt(debt["id"])
            await send_simple_message(
                update,
                context,
                f"✅ Долг *{debt['amount']:.2f} ₽* за *{debt['reason']}* погашен и закрыт.",
            )
        else:
            # Если долг погашен частично, сообщаем об остатке
            await send_simple_message(
                update,
                context,
                f"Вычтено *{amount_to_subtract:.2f} ₽*. Остаток долга: *{new_amount:.2f} ₽*.",
            )
        await show_debtor_details(
            update, context, debt["debtor_id"]
        )  # Обновляем отображение деталей должника
        clear_user_state(chat_id)  # Сбрасываем состояние

    elif state == STATE_SETTING_PAYMENT_DATE:
        # Устанавливаем дату платежа
        try:
            # Пробуем разные форматы даты
            date_formats = ["%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d-%m-%y"]
            payment_date = None
            for fmt in date_formats:
                try:
                    payment_date = datetime.strptime(text, fmt)
                    break  # Если дата успешно разобрана, выходим из цикла
                except ValueError:
                    continue  # Если формат не подошел, пробуем следующий
            if payment_date is None:
                # Если ни один формат не подошел, выбрасываем исключение
                raise ValueError("Invalid date format")
        except ValueError:
            await send_simple_message(
                update,
                context,
                "Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ или ДД.ММ.ГГ.",
            )
            return

        debtor_id = current_debtors[chat_id]["id"]  # Получаем ID текущего должника
        update_debtor_payment_date(debtor_id, payment_date) # Обновляем дату в БД.
        await send_simple_message(
            update,
            context,
            f"Дата платежа для *{current_debtors[chat_id]['name']}* установлена на *{payment_date.strftime('%d.%m.%Y')}*.",
        )
        await show_debtor_details(update, context, debtor_id)
        clear_user_state(chat_id)

    elif state == STATE_SETTING_PAYMENT_AMOUNT:
        # Устанавливаем сумму платежа
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
        # Редактируем дату платежа
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
        # Редактируем сумму
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
        # Если состояние неизвестно, выводим сообщение об ошибке и сбрасываем состояние
        await send_simple_message(
            update,
            context,
            "Используй /add для добавления долга, /debts для просмотра долгов.",
        )
        clear_user_state(chat_id)


# Обработчик нажатий на кнопки инлайн-клавиатуры
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки инлайн-клавиатуры."""
    query = update.callback_query
    await query.answer()  # Обязательно отвечаем на callback query!
    data = query.data  # Получаем данные, связанные с кнопкой
    chat_id = query.message.chat_id

    if data.startswith("select_debtor:"):
        # Обработка выбора должника из списка
        debtor_id = int(data.split(":")[1])  # Извлекаем ID должника из данных
        debtor = get_debtor_by_id(debtor_id)  # Получаем информацию о должнике по ID
        if not debtor:
            # Если должник не найден (например, был удален), сообщаем об этом
            await context.bot.send_message(chat_id=chat_id, text="Должник не найден.")
            clear_user_state(chat_id)
            return

        current_debtors[chat_id] = debtor  # Сохраняем информацию о выбранном должнике
        clear_user_state(chat_id)  # Сбрасываем состояние (но сохраняем current_debtor)
        await show_debtor_details(update, context, debtor_id)  # Отображаем детализацию долгов должника

    elif data.startswith("close_debt:"):
        # Обработка нажатия на кнопку "Закрыть долг"
        debt_id = int(data.split(":")[1])
        debt = get_debt_by_id(debt_id)  # Получаем информацию о долге по ID
        if not debt:
            await context.bot.send_message(
                chat_id=chat_id, text="Долг не найден."
            )  # Информируем если не нашли
            return  # Выходим

        selected_debts[chat_id] = debt  # Сохраняем информацию о выбранном долге
        user_states[chat_id] = STATE_CONFIRMING_CLOSE_DEBT  # Переводим пользователя в состояние подтверждения закрытия

        # Создаем клавиатуру с кнопками "Да" и "Отмена"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да, закрыть", callback_data=f"confirm_close:{debt_id}"),
                    InlineKeyboardButton("❌ Отмена", callback_data="cancel_operation"),
                ]
            ]
        )
        # Запрашиваем подтверждение закрытия долга
        await edit_message_with_keyboard(
            update,
            context,
            f"Вы уверены, что хотите закрыть долг *{debt['amount']:.2f} ₽* за *{debt['reason']}*?",
            keyboard,
        )

    elif data.startswith("confirm_close:"):
        # Обработка подтверждения закрытия долга
        debt_id = int(data.split(":")[1])
        close_debt(debt_id)  # Закрываем (удаляем) долг
        await edit_message_with_keyboard(update, context, "Долг закрыт.")
        if chat_id in current_debtors: # Проверка
          await show_debtor_details(update, context, current_debtors[chat_id]["id"]) # Обновляем инфо
        clear_user_state(chat_id)  # Сбрасываем состояние


    elif data == "cancel_operation":
        # Обработка отмены операции (например, закрытия долга или удаления должника)
        await edit_message_with_keyboard(update, context, "Операция отменена.")
        if chat_id in current_debtors:  # Проверяем наличие ключа
          await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        clear_user_state(chat_id)

    elif data.startswith("edit_debt:"):
        # Обработка нажатия на кнопку "Редактировать долг"
        debt_id = int(data.split(":")[1])
        debt = get_debt_by_id(debt_id)
        if not debt:
            await context.bot.send_message(chat_id=chat_id, text="Долг не найден.")
            return

        selected_debts[chat_id] = debt  # Сохраняем информацию о выбранном долге
        user_states[chat_id] = STATE_EDITING_CHOOSE_WHAT_TO_EDIT  # Переводим в состояние выбора, что редактировать

        # Создаем клавиатуру с кнопками "Изменить сумму", "Изменить причину" и "Вычесть из долга"
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
        # Обработка выбора редактирования суммы долга
        debt_id = int(data.split(":")[1])
        selected_debts[chat_id] = {"id": debt_id}  # Сохраняем только ID долга
        user_states[chat_id] = STATE_EDITING_AMOUNT  # Переводим в состояние ввода новой суммы
        await edit_message_with_keyboard(update, context, "Введите новую сумму:")

    elif data.startswith("edit_reason:"):
        # Обработка выбора редактирования причины долга
        debt_id = int(data.split(":")[1])
        selected_debts[chat_id] = {"id": debt_id}  # Сохраняем только ID долга
        user_states[chat_id] = STATE_EDITING_REASON  # Переводим в состояние ввода новой причины
        await edit_message_with_keyboard(update, context, "Введите новую причину:")

    elif data.startswith("subtract_from_debt:"):
        # Обработка выбора вычитания из долга
        debt_id = int(data.split(":")[1])
        debt = get_debt_by_id(debt_id)
        if not debt:
            await context.bot.send_message(chat_id=chat_id, text="Долг не найден.")
            return
        selected_debts[chat_id] = debt  # Сохраняем полную информацию.
        user_states[chat_id] = STATE_SUBTRACTING_FROM_DEBT  # Переводим в состояние ввода суммы для вычитания
        await edit_message_with_keyboard(
            update, context, f"Какую сумму вычесть из долга *{debt['amount']:.2f} ₽*?"
        )
    elif data == "add_debt_to_existing":
        # Добавление нового долга к *существующему* должнику
        user_states[chat_id] = STATE_ADDING_DEBT_REASON  # Переводим в состояние ввода причины
        await edit_message_with_keyboard(
            update,
            context,
            f"Какова причина долга для *{current_debtors[chat_id]['name']}*?",
        )

    elif data == "delete_debtor":
        # Обработка нажатия на кнопку "Удалить должника"
        user_states[chat_id] = STATE_CONFIRMING_DELETE_DEBTOR  # Переводим в состояние подтверждения удаления

        # Создаем клавиатуру с кнопками "Да" и "Отмена"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да, удалить", callback_data="confirm_delete_debtor"),
                    InlineKeyboardButton("❌ Отмена", callback_data="cancel_operation"),
                ]
            ]
        )
        # Запрашиваем подтверждение удаления должника
        await edit_message_with_keyboard(
            update,
            context,
            f"Вы уверены, что хотите удалить должника *{current_debtors[chat_id]['name']}*? *Все долги этого должника будут удалены!*",
            keyboard,
        )

    elif data == "confirm_delete_debtor":
        # Обработка подтверждения удаления должника
        debtor_id = current_debtors[chat_id]["id"]
        delete_debtor(debtor_id)  # Удаляем должника (и все его долги) из базы данных
        await edit_message_with_keyboard(
            update, context, f"Должник *{current_debtors[chat_id]['name']}* и все долги удалены."
        )
        current_debtors.pop(chat_id, None)  # Удаляем информацию о должнике из памяти
        clear_user_state(chat_id)  # Сбрасываем состояние

    elif data == "set_payment_date":
        # Устанавливаем дату
        user_states[chat_id] = STATE_SETTING_PAYMENT_DATE
        await edit_message_with_keyboard(
            update, context, "Введите дату платежа (ДД.ММ.ГГГГ или ДД.ММ.ГГ):"
        )

    elif data == "set_payment_amount":
        # Устанавливаем сумму
        user_states[chat_id] = STATE_SETTING_PAYMENT_AMOUNT
        await edit_message_with_keyboard(update, context, "Введите сумму платежа:")

    elif data == "clear_payment_date":
        # Очистка даты
        if chat_id in current_debtors:  # Проверяем, есть ли текущий должник
            clear_debtor_payment_date(current_debtors[chat_id]["id"])
            await edit_message_with_keyboard(update, context, "Дата платежа очищена.")
            await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        else:
            # Если текущего должника нет (например, после удаления), сообщаем об ошибке
            await context.bot.send_message(chat_id=chat_id, text="Ошибка: нет текущего должника.")
        clear_user_state(chat_id)

    elif data == "clear_payment_amount":
        # Очистка суммы
        if chat_id in current_debtors:
            clear_debtor_payment_amount(current_debtors[chat_id]["id"])
            await edit_message_with_keyboard(update, context, "Сумма платежа очищена.")
            await show_debtor_details(update, context, current_debtors[chat_id]["id"])
        else:
            await context.bot.send_message(chat_id=chat_id, text="Ошибка: нет текущего должника.")
        clear_user_state(chat_id)

    elif data == "edit_payment_date":
        # Редактирование даты
        user_states[chat_id] = STATE_EDITING_PAYMENT_DATE
        await edit_message_with_keyboard(
            update, context, "Введите новую дату платежа (ДД.ММ.ГГГГ или ДД.ММ.ГГ):"
        )

    elif data == "edit_payment_amount":
        # Редактирвоание суммы
        user_states[chat_id] = STATE_EDITING_PAYMENT_AMOUNT
        await edit_message_with_keyboard(update, context, "Введите новую сумму платежа:")


# Функция для отображения детальной информации о должнике и его долгах
async def show_debtor_details(
    update: Update, context: ContextTypes.DEFAULT_TYPE, debtor_id: int
):
    """Отображает детальную информацию о должнике, его долгах и предоставляет кнопки для управления."""
    debtor = get_debtor_by_id(debtor_id)  # Получаем информацию о должнике по ID
    if not debtor:
        # Если должник не найден, сообщаем об этом пользователю
        if update.callback_query:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text="Должник не найден."
            )  # Используем effective_chat
        else:
            await update.message.reply_text("Должник не найден.")
        return

    chat_id = (
        update.effective_chat.id
    )  # Получаем chat_id *перед* любыми сбросами состояния
    current_debtors[chat_id] = debtor  # *Всегда* сохраняем информацию о текущем должнике

    debts = list_debts(debtor_id)  # Получаем список долгов должника
    total_debt = sum(debt["amount"] for debt in debts)  # Вычисляем общую сумму долга

    # Формируем текст сообщения с информацией о долгах
    debts_text = f"*Долги {debtor['name']}:*\n\n"
    keyboard_buttons = []  # Создаем список для кнопок инлайн-клавиатуры

    for debt in debts:
        # Добавляем информацию о каждом долге в текст сообщения
        debts_text += f"- *{debt['amount']:.2f} ₽* за *{debt['reason']}*\n"
        # Добавляем кнопки "Редактировать" и "Закрыть" для каждого долга
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

    # Добавляем информацию о дате и сумме платежа, если они установлены
    if debtor.get("payment_date"):
        debts_text += f"\n\n*Дата платежа:* {datetime.strptime(str(debtor['payment_date']), '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y')}"
        # Добавляем кнопки для управления датой платежа
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
        # Если дата платежа не установлена, добавляем кнопку "Указать дату"
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "Указать дату платежа", callback_data="set_payment_date"
                ),
            ]
        )

    if debtor.get("payment_amount"):
        debts_text += f"\n*Сумма платежа:* {debtor['payment_amount']:.2f} ₽"
        # Добавляем кнопки для управления суммой платежа
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
        # Если сумма платежа не установлена, добавляем кнопку "Указать сумму"
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "Указать сумму платежа", callback_data="set_payment_amount"
                ),
            ]
        )

    # Добавляем кнопки "Добавить долг" и "Удалить должника"
    keyboard_buttons.append(
        [
            InlineKeyboardButton("➕ Добавить долг", callback_data="add_debt_to_existing"),
            InlineKeyboardButton("🗑️ Удалить должника", callback_data="delete_debtor"),
        ]
    )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)  # Создаем инлайн-клавиатуру из списка кнопок

    # Отправляем или редактируем сообщение с деталями должника и кнопками
    if update.callback_query:
        await edit_message_with_keyboard(update, context, debts_text, keyboard)
    else:
        await send_with_keyboard(update, context, debts_text, keyboard)



def main():
    """Основная функция, запускающая бота."""
    load_dotenv()  # Загружаем переменные окружения из файла .env
    bot_token = os.getenv("TELEGRAM_API_TOKEN")  # Получаем токен бота из переменной окружения
    if not bot_token:
        # Если токен не найден, выводим сообщение об ошибке и завершаем работу
        logger.error("TELEGRAM_API_TOKEN not found in environment variables.")
        return

    init_db()  # Инициализируем базу данных

    # Создаем объект Application с помощью ApplicationBuilder (более современный подход)
    app = ApplicationBuilder().token(bot_token).build()

    # Регистрируем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("debts", debts))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("exportcsv", exportcsv))

    # Регистрируем обработчик текстовых сообщений (для ввода данных)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Регистрируем обработчик callback query (для нажатий на кнопки)
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Запускаем бота (polling - бот постоянно опрашивает сервер Telegram на наличие обновлений)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()  # Запускаем основную функцию