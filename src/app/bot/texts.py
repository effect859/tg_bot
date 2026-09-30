"""Тексты бота. Сообщения отправляются с parse_mode=HTML: пользовательские данные экранируем."""

from html import escape

from app.db.models import Order
from app.domain.enums import DocType, OrderStatus
from app.domain.errors import (
    ConsentNotGivenError,
    MaterialLimitError,
    MaterialRejectReason,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
    OrderDomainError,
    UnsupportedMaterialError,
)
from app.domain.materials import MAX_MATERIAL_SIZE_BYTES, MAX_MATERIALS_PER_ORDER

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

BTN_CONSENT = "✅ Согласен"
BTN_CANCEL_ORDER = "✖ Отменить заказ"

MAX_MATERIAL_SIZE_MIB = MAX_MATERIAL_SIZE_BYTES // (1024 * 1024)
# Сколько символов имени файла показываем в ответах.
FILENAME_PREVIEW_LENGTH = 60

# Черновик: юридическую формулировку согласия нужно утвердить отдельно.
CONSENT_REQUEST = (
    "<b>Новый отчёт по практике</b>\n\n"
    "Чтобы подготовить отчёт, бот сохранит присланные вами файлы и данные заказа "
    "и будет использовать их только для выполнения этого заказа.\n\n"
    "Нажмите «Согласен», чтобы продолжить."
)

FILES_REQUEST = (
    "Пришлите материалы для отчёта: задание на практику, "
    "методические указания, примеры.\n\n"
    "Форматы: PDF, DOCX или TXT — каждый файл отдельным документом, "
    f"до {MAX_MATERIAL_SIZE_MIB} МиБ, не больше {MAX_MATERIALS_PER_ORDER} файлов."
)

NO_ACTIVE_ORDER = f"У вас нет активного заказа. Нажмите «{BTN_NEW_REPORT}», чтобы начать."
ORDER_NOT_FOUND = "Заказ не найден."
ORDER_CREATE_FAILED = "Не удалось создать заказ. Попробуйте ещё раз."

MATERIAL_UNKNOWN_FORMAT = "формат не поддерживается. Пришлите PDF, DOCX или TXT."
MATERIAL_BAD_FILENAME = "у файла некорректное имя. Переименуйте файл и пришлите его снова."
MATERIAL_MIME_MISMATCH = (
    "тип файла не совпадает с расширением. Проверьте файл и пришлите его снова."
)
MATERIAL_BAD_CONTENT = (
    "содержимое файла не соответствует расширению или файл повреждён. "
    "Проверьте файл и пришлите его снова."
)
MATERIAL_TOO_LARGE = f"файл слишком большой: максимум {MAX_MATERIAL_SIZE_MIB} МиБ."
MATERIAL_EMPTY = "файл пустой."
MATERIAL_LIMIT = f"достигнут лимит: не больше {MAX_MATERIALS_PER_ORDER} файлов на заказ."
MATERIAL_NO_CONSENT = "сначала подтвердите согласие — нажмите «Согласен»."
MATERIAL_LOCKED = "данные заказа уже подтверждены, новые файлы добавить нельзя."
MATERIAL_REJECTED = "файл не принят."
MATERIAL_DOWNLOAD_FAILED = "не удалось получить файл из Telegram. Отправьте его ещё раз."
MATERIAL_STORAGE_FAILED = "не удалось сохранить файл. Попробуйте позже."
MATERIAL_DB_FAILED = "техническая ошибка, файл не принят. Попробуйте позже."
MATERIAL_COMMIT_UNKNOWN = (
    "техническая ошибка при сохранении: не удалось подтвердить, что файл принят. "
    "Отправьте его ещё раз."
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


def status_label(status: OrderStatus) -> str:
    return ORDER_STATUS_LABELS.get(status, status)


def active_order_exists(order: Order) -> str:
    return (
        f"У вас уже есть активный заказ #{order.id} ({status_label(order.status)}). "
        "Новый можно оформить после его завершения или отмены."
    )


def waiting_for_files(order: Order) -> str:
    return f"Заказ #{order.id} ждёт материалы.\n\n{FILES_REQUEST}"


def order_cancelled(order: Order) -> str:
    return f"Заказ #{order.id} отменён."


def action_outdated(order: Order) -> str:
    return f"Действие уже неактуально: заказ #{order.id} — «{status_label(order.status)}»."


def file_label(filename: str | None) -> str:
    """Имя файла для ответа: укороченное и экранированное для HTML."""
    name = (filename or "").strip()
    if not name:
        return "без имени"
    if len(name) > FILENAME_PREVIEW_LENGTH:
        name = name[: FILENAME_PREVIEW_LENGTH - 1] + "…"
    # Обрезаем до экранирования, чтобы не разрезать HTML-сущность.
    return escape(name)


def material_accepted(label: str, count: int) -> str:
    return f"✅ Файл «{label}» принят ({count} из {MAX_MATERIALS_PER_ORDER})."


def material_failed(label: str, reason: str) -> str:
    return f"❌ Файл «{label}»: {reason}"


def material_rejection(error: OrderDomainError) -> str:
    """Причина отказа в приёме файла — без технических подробностей."""
    match error:
        case UnsupportedMaterialError(reason=MaterialRejectReason.FORMAT):
            return MATERIAL_UNKNOWN_FORMAT
        case UnsupportedMaterialError(reason=MaterialRejectReason.FILENAME):
            return MATERIAL_BAD_FILENAME
        case UnsupportedMaterialError(reason=MaterialRejectReason.MIME):
            return MATERIAL_MIME_MISMATCH
        case UnsupportedMaterialError(reason=MaterialRejectReason.CONTENT):
            return MATERIAL_BAD_CONTENT
        case UnsupportedMaterialError(reason=MaterialRejectReason.EMPTY):
            return MATERIAL_EMPTY
        case UnsupportedMaterialError(reason=MaterialRejectReason.SIZE):
            return MATERIAL_TOO_LARGE
        case MaterialLimitError():
            return MATERIAL_LIMIT
        case ConsentNotGivenError():
            return MATERIAL_NO_CONSENT
        case MaterialsLockedError():
            return MATERIAL_LOCKED
        case MaterialsNotAcceptedError(status=status):
            return f"сейчас файлы не принимаются: заказ на шаге «{status_label(status)}»."
        case _:
            return MATERIAL_REJECTED
