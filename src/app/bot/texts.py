"""Тексты бота. Сообщения отправляются с parse_mode=HTML: пользовательские данные экранируем."""

from html import escape

from app.db.models import Order
from app.domain.enums import DocType, OrderStatus

BTN_NEW_REPORT = "📝 Новый отчёт по практике"
BTN_MY_ORDERS = "📂 Мои заказы"
BTN_BALANCE = "💰 Баланс"
BTN_HELP = "❓ Помощь"

DOC_TYPE_LABELS: dict[DocType, str] = {
    DocType.PRACTICE_REPORT: "Отчёт по практике",
}

ORDER_STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.NEW: "новый",
    OrderStatus.WAITING_FOR_CONSENT: "ожидает согласия",
    OrderStatus.WAITING_FOR_FILES: "ожидает материалы",
    OrderStatus.ANALYZING_MATERIALS: "анализ материалов",
    OrderStatus.WAITING_FOR_MISSING_DATA: "нужны данные",
    OrderStatus.WAITING_FOR_DATA_CONFIRMATION: "подтверждение данных",
    OrderStatus.PREPARING_PLAN: "составляется план",
    OrderStatus.WAITING_FOR_PLAN_CONFIRMATION: "подтверждение плана",
    OrderStatus.PAYMENT_PENDING: "ожидает оплаты",
    OrderStatus.PAID: "оплачен",
    OrderStatus.GENERATING: "генерируется",
    OrderStatus.COMPLETED: "готов",
    OrderStatus.REVISION_REQUESTED: "запрошена доработка",
    OrderStatus.REGENERATING: "дорабатывается",
    OrderStatus.FAILED: "ошибка",
    OrderStatus.CANCELLED: "отменён",
}

NEW_REPORT_STUB = (
    "Создание отчёта по практике скоро будет доступно. "
    "Мы сообщим, когда можно будет оформить первый заказ."
)

NO_ORDERS = "У вас пока нет заказов."

HELP = (
    "<b>Как это работает</b>\n"
    "1. Вы присылаете задание на практику и отвечаете на пару вопросов.\n"
    "2. Бот бесплатно составляет план отчёта.\n"
    "3. После оплаты вы получаете готовый DOCX, оформленный по ГОСТу.\n\n"
    "Команды: /start — главное меню, /help — эта справка."
)


def start_greeting(first_name: str | None) -> str:
    name = escape(first_name) if first_name else "студент"
    return (
        f"Привет, {name}! 👋\n\n"
        "Я помогаю подготовить отчёт по практике: разберу задание, "
        "составлю план и оформлю документ в DOCX.\n\n"
        "Выберите действие в меню ниже."
    )


def format_money(kopecks: int) -> str:
    rubles, kop = divmod(kopecks, 100)
    return f"{rubles} ₽" if kop == 0 else f"{rubles},{kop:02d} ₽"


def balance(kopecks: int) -> str:
    return f"Ваш баланс: <b>{format_money(kopecks)}</b>"


def orders_list(orders: list[Order]) -> str:
    lines = ["<b>Последние заказы</b>"]
    for order in orders:
        created = order.created_at.strftime("%d.%m.%Y")
        doc = DOC_TYPE_LABELS.get(order.doc_type, order.doc_type)
        status = ORDER_STATUS_LABELS.get(order.status, order.status)
        lines.append(f"#{order.id} · {doc} · {status} · {created}")
    return "\n".join(lines)
