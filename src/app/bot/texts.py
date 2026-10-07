"""Тексты бота. Сообщения отправляются с parse_mode=HTML: пользовательские данные экранируем."""

from collections.abc import Sequence
from datetime import date
from html import escape

from app.db.models import Order
from app.domain.enums import DocType, MaterialFailureReason, OrderStatus, TextTruncationReason
from app.domain.errors import (
    ConsentNotGivenError,
    MaterialLimitError,
    MaterialRejectReason,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
    MaterialsNotReadyError,
    MaterialsNotReadyReason,
    OrderDomainError,
    UnsupportedMaterialError,
)
from app.domain.materials import (
    MAX_MATERIAL_SIZE_BYTES,
    MAX_MATERIALS_PER_ORDER,
    MaterialCompletionState,
)
from app.domain.requirements import MAX_ANALYSIS_INPUT_CHARS, REQUIRED_FIELDS
from app.domain.requirements_dialog import TEXT_ANSWER_LIMITS, AnswerError
from app.extraction.base import WarningCode
from app.services.material_processing import OutcomeKind, ProcessingOutcome
from app.services.order_materials import MaterialsCompletion
from app.services.requirements_analysis import AnalysisOutcome

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
    # Подтверждение плана — следующий этап: пока план только показан.
    OrderStatus.WAITING_FOR_PLAN_CONFIRMATION: "план готов",
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
FINISH_MATERIALS = "Материалы загружены"
BTN_FINISH_MATERIALS = f"✅ {FINISH_MATERIALS}"
RETRY_ANALYSIS = "Повторить анализ"
BTN_RETRY_ANALYSIS = f"🔄 {RETRY_ANALYSIS}"
RETRY_PLAN = "Повторить составление плана"
BTN_RETRY_PLAN = f"🔄 {RETRY_PLAN}"
CONFIRM_PLAN = "Подтвердить план"
BTN_CONFIRM_PLAN = f"✅ {CONFIRM_PLAN}"

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

MATERIAL_PROCESSING_DEFERRED = (
    "⏳ Не удалось обработать файлы из-за технической ошибки. "
    "Повторим автоматически и сообщим результат."
)
TEXT_NOT_FOUND = "текст не найден."
TEXT_LOOKS_SCANNED = (
    " Похоже, это скан или изображения: распознавание текста с картинок пока не поддерживается."
)
TEXT_SEND_TEXT_VERSION = (
    " Пришлите версию с текстом: DOCX, TXT или PDF, сохранённый из текстового редактора."
)
TEXT_LOW_QUALITY = (
    " Часть текста может быть извлечена неверно — если есть DOCX-версия, пришлите её."
)
TEXT_CHAR_LIMIT = " Документ очень большой: сохранена только его начальная часть."
TEXT_TIME_BUDGET = " Документ обрабатывался слишком долго: сохранена только его часть."
MAX_LISTED_PAGES = 10

MATERIAL_RETRY_LATER = "временная ошибка обработки. Повторим автоматически и сообщим результат."
MATERIAL_PROCESSING_FAILED = "не удалось обработать файл. Пришлите его ещё раз."
MATERIAL_FAILURE_TEXTS: dict[MaterialFailureReason, str] = {
    MaterialFailureReason.FILE_MISSING: "файл не найден в хранилище. Пришлите его ещё раз.",
    MaterialFailureReason.CORRUPTED: (
        "файл повреждён или не читается. Проверьте его и пришлите заново."
    ),
    MaterialFailureReason.ENCRYPTED: (
        "файл защищён паролем. Снимите защиту и пришлите его заново."
    ),
    MaterialFailureReason.EMPTY_DOCUMENT: "в документе нет ни одной страницы.",
    MaterialFailureReason.UNSUPPORTED: (
        "документ содержит неподдерживаемые элементы. Пересохраните его в Word или "
        "LibreOffice и пришлите заново."
    ),
    MaterialFailureReason.LIMIT_EXCEEDED: (
        "документ слишком объёмный или сложный для обработки. Сохраните его в "
        "упрощённом виде и пришлите заново."
    ),
    MaterialFailureReason.TIMEOUT: (
        "документ обрабатывался слишком долго. Сохраните его в упрощённом виде и пришлите заново."
    ),
    MaterialFailureReason.EXTRACTOR_CRASH: (
        "не удалось извлечь текст. Пересохраните файл и пришлите его заново."
    ),
    MaterialFailureReason.EXTRACTOR_ERROR: (
        "не удалось извлечь текст. Пересохраните файл и пришлите его заново."
    ),
    MaterialFailureReason.RESULT_REJECTED: (
        "не удалось сохранить извлечённый текст. Пересохраните файл и пришлите его заново."
    ),
}

MATERIALS_NONE = "Вы ещё не прислали ни одного файла. Пришлите материалы — PDF, DOCX или TXT."
MATERIALS_NO_TEXT = (
    "Ни из одного файла не удалось извлечь текст. Пришлите материалы с текстом: "
    "DOCX, TXT или PDF, сохранённый из текстового редактора."
)
MATERIALS_NEXT_STEP = "Следующий шаг — анализ материалов. Сообщим, когда он будет готов."

# Поля ТЗ отчёта по практике — как их называть пользователю.
REQUIREMENT_LABELS: dict[str, str] = {
    "topic": "тема практики",
    "practice_type": "вид практики",
    "practice_place": "место практики",
    "practice_start": "дата начала практики",
    "practice_end": "дата окончания практики",
    "university": "вуз",
    "specialty": "направление подготовки",
    "student_name": "ФИО студента",
    "student_group": "группа",
    "supervisor": "руководитель практики",
    "target_pages": "объём отчёта",
    "extra_requirements": "дополнительные требования",
}
ANALYSIS_STARTED = "🔎 Анализирую материалы — это может занять пару минут."
ANALYSIS_DELAYED = (
    "⏳ Анализ материалов не завершился с первой попытки. Повторим автоматически "
    "и сообщим результат."
)
ANALYSIS_LIMIT_REACHED = "Лимит попыток анализа исчерпан. Отмените заказ, чтобы оформить новый."
ANALYSIS_UNAVAILABLE = "Анализ материалов сейчас недоступен. Попробуйте позже."

# --- Уточнение и подтверждение ТЗ ---------------------------------------------------

BTN_CONFIRM_REQUIREMENTS = "✅ Подтвердить ТЗ"
BTN_EDIT_REQUIREMENTS = "✏️ Изменить"
# Даты меняются вместе: одна кнопка на обе.
BTN_EDIT_DATES = "Даты практики"

DATE_INPUT_HINT = "в формате ДД.ММ.ГГГГ, например 01.07.2026"
# Вопросы по обязательным полям — по одному, в порядке REQUIRED_FIELDS.
REQUIREMENT_QUESTIONS: dict[str, str] = {
    "topic": "Какая тема практики (или индивидуального задания)?",
    "practice_type": "Какой вид практики? Например: учебная, производственная, преддипломная.",
    "practice_place": "Где вы проходите практику? Укажите организацию или подразделение.",
    "practice_start": f"Когда началась практика? Укажите дату {DATE_INPUT_HINT}.",
    "practice_end": f"Когда заканчивается практика? Укажите дату {DATE_INPUT_HINT}.",
    "university": "В каком вузе вы учитесь? Укажите полное название.",
    "student_name": "Как вас зовут? Укажите фамилию, имя и отчество.",
}
ANSWER_ERROR_TEXTS: dict[AnswerError, str] = {
    AnswerError.EMPTY: "Ответ пустой.",
    AnswerError.INVALID_CHARS: "В ответе есть недопустимые символы.",
    AnswerError.NO_LETTERS: "Ответ должен содержать хотя бы две буквы.",
    AnswerError.DIGITS_IN_NAME: "В ФИО не должно быть цифр.",
    AnswerError.INVALID_DATE: f"Не удалось распознать дату. Укажите её {DATE_INPUT_HINT}.",
    AnswerError.DATE_OUT_OF_RANGE: "Дата должна быть в пределах 2000–2100 годов.",
}
DATES_RESET = "Дата начала позже даты окончания. Укажите даты заново."
ANSWER_FAILED = "Не удалось сохранить ответ из-за технической ошибки. Отправьте его ещё раз."
REQUIREMENTS_UNAVAILABLE = "Не удалось открыть ТЗ из-за технической ошибки. Попробуйте позже."
REQUIREMENTS_ACTION_FAILED = (
    "Не удалось выполнить действие из-за технической ошибки. Попробуйте ещё раз."
)
REQUIREMENTS_EDIT_CHOOSE = "Что изменить? Выберите поле — я задам вопрос о нём заново."
REQUIREMENT_NOT_EDITABLE = "Это поле нельзя изменить."
REQUIREMENTS_CHANGED = "ТЗ изменилось после этого сообщения. Проверьте актуальную версию."
REQUIREMENTS_ALREADY_CONFIRMED = "ТЗ уже подтверждено."
REQUIREMENTS_CONFIRMED = "ТЗ подтверждено. Следующий шаг — подготовка плана отчёта."
REQUIREMENTS_CONFIRM_PROMPT = "Проверьте данные. Если всё верно — подтвердите ТЗ."

# Лимит Telegram на длину сообщения. Резюме ТЗ укладывается в него при любых данных.
MAX_MESSAGE_CHARS = 4096
# Шаги сокращения резюме: (символов на значение, доптребований), от подробного к краткому.
SUMMARY_LIMITS: tuple[tuple[int, int], ...] = ((500, 10), (200, 5), (100, 3), (40, 1))
# Необязательные поля в резюме — то, что нашёл анализ; редактировать их нельзя.
SUMMARY_OPTIONAL_FIELDS: tuple[str, ...] = (
    "specialty",
    "student_group",
    "supervisor",
    "target_pages",
)
# Сколько символов значения показывать в подтверждении ответа.
ANSWER_PREVIEW_LENGTH = 200

# --- План отчёта --------------------------------------------------------------------

PLAN_STARTED = "🗂 Составляю план отчёта — это может занять пару минут."
PLAN_DELAYED = (
    "⏳ План не удалось составить с первой попытки. Повторим автоматически и сообщим результат."
)
PLAN_LIMIT_REACHED = (
    "Лимит попыток составления плана исчерпан. Отмените заказ, чтобы оформить новый."
)
PLAN_UNAVAILABLE = "Составление плана сейчас недоступно. Попробуйте позже."
PLAN_SHOW_FAILED = "Не удалось показать план из-за технической ошибки. Попробуйте позже."
PLAN_ACTION_FAILED = "Не удалось выполнить действие из-за технической ошибки. Попробуйте ещё раз."
PLAN_NEXT_STEP = (
    f"Если план подходит, нажмите «{CONFIRM_PLAN}». Оплата и подготовка отчёта пока "
    "недоступны. Заказ можно отменить."
)
PLAN_ALREADY_CONFIRMED = "План уже подтверждён."
PLAN_CHANGED = "План изменился после этого сообщения. Проверьте актуальную версию."
PLAN_CONFIRM_FAILED = "Не удалось подтвердить план из-за технической ошибки. Попробуйте позже."
PLAN_SHORTENED = "<i>План показан сокращённо.</i>"
# Шаги сокращения плана: (символов в названии раздела, в описании, требований,
# символов в требовании, подразделов, символов в названии подраздела).
PLAN_VIEW_LIMITS: tuple[tuple[int, int, int, int, int, int], ...] = (
    (200, 300, 10, 200, 10, 200),
    (150, 150, 3, 120, 10, 120),
    (100, 0, 0, 0, 10, 80),
    (80, 0, 0, 0, 3, 60),
    (30, 0, 0, 0, 0, 0),
)

NO_ORDERS = "У вас пока нет заказов."

BTN_DOWNLOAD_REPORT = "📄 Скачать отчёт"
# Подпись документа при фоновой доставке: номер заказа — в имени файла.
REPORT_DELIVERED = f"✅ Ваш отчёт готов. Скачать его снова можно в «{BTN_MY_ORDERS}»."
REPORT_NOT_READY = "Отчёт по этому заказу ещё не готов."
REPORT_UNAVAILABLE = "Файл отчёта сейчас недоступен."
REPORT_DOWNLOAD_FAILED = "Не удалось отправить отчёт из-за технической ошибки. Попробуйте позже."

HELP = (
    "<b>Как это работает</b>\n"
    "1. Вы присылаете задание на практику и отвечаете на пару вопросов.\n"
    "2. Бот бесплатно составляет план отчёта.\n"
    "3. Оплата и готовый DOCX, оформленный по ГОСТу, пока в разработке.\n\n"
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


def download_report_button(order_id: int) -> str:
    return f"{BTN_DOWNLOAD_REPORT} #{order_id}"


def report_ready(order_id: int) -> str:
    return f"✅ Отчёт по заказу #{order_id}"


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


def materials_not_ready(error: MaterialsNotReadyError) -> str:
    """Почему приём материалов нельзя завершить — коротко, для всплывающего окна."""
    match error.reason:
        case MaterialsNotReadyReason.NO_MATERIALS:
            return MATERIALS_NONE
        case MaterialsNotReadyReason.PENDING:
            return (
                f"⏳ Ещё обрабатываются файлы: {error.pending}. Дождитесь сообщений о "
                f"результате и нажмите «{FINISH_MATERIALS}» снова."
            )
        case _:
            return MATERIALS_NO_TEXT


def materials_already_finished(order: Order) -> str:
    return f"Материалы по заказу #{order.id} уже приняты."


def _labels(fields: Sequence[str]) -> str:
    return ", ".join(REQUIREMENT_LABELS.get(name, name) for name in fields)


def analysis_completed(outcome: AnalysisOutcome) -> str:
    """Итог анализа: что найдено в материалах и чего не хватает — без технических деталей."""
    lines = [f"📋 Материалы по заказу #{outcome.order_id} проанализированы."]
    lines.append(
        f"Найдено в материалах: {_labels(outcome.found)}."
        if outcome.found
        else "В материалах не нашлось данных для ТЗ."
    )
    if outcome.missing:
        lines.append(f"Нужно уточнить: {_labels(outcome.missing)}.")
    if outcome.truncated_material is not None or outcome.omitted_materials:
        details = []
        if outcome.truncated_material is not None:
            details.append(f"файл №{outcome.truncated_material} — частично")
        if outcome.omitted_materials:
            numbers = ", ".join(f"№{n}" for n in outcome.omitted_materials)
            details.append(f"не вошли: {numbers}")
        limit = f"{MAX_ANALYSIS_INPUT_CHARS:,}".replace(",", " ")
        lines.append(
            f"⚠️ Материалы слишком объёмные: учтены первые {limit} символов ({'; '.join(details)})."
        )
    lines.append("")
    if outcome.status is OrderStatus.WAITING_FOR_DATA_CONFIRMATION:
        lines.append(
            "Все данные для ТЗ найдены. Чтобы проверить и подтвердить ТЗ, отправьте /start."
        )
    else:
        lines.append("Осталось уточнить несколько данных — задам вопросы по одному.")
    return "\n".join(lines)


def field_label(field: str) -> str:
    """Название поля ТЗ с заглавной буквы — для резюме и кнопок."""
    label = REQUIREMENT_LABELS.get(field, field)
    return label[:1].upper() + label[1:]


def question(field: str) -> str:
    return f"❓ {REQUIREMENT_QUESTIONS[field]}"


def question_reminder(order_id: int, field: str) -> str:
    return f"Заказ #{order_id}: нужно уточнить данные для ТЗ.\n\n{question(field)}"


def answer_rejected(field: str, error: AnswerError) -> str:
    if error is AnswerError.TOO_LONG:
        reason = f"Слишком длинный ответ: не больше {TEXT_ANSWER_LIMITS[field]} символов."
    else:
        reason = ANSWER_ERROR_TEXTS[error]
    return f"⚠️ {reason}\n\n{question(field)}"


def answer_accepted(field: str, value: str) -> str:
    """Что записано — чтобы ответ, попавший не в то поле, было видно сразу."""
    shown = _field_value(field, value, ANSWER_PREVIEW_LENGTH)
    return f"✅ Записал: {REQUIREMENT_LABELS.get(field, field)} — {shown}."


def plan_in_progress(order_id: int) -> str:
    return (
        f"Заказ #{order_id}: ТЗ подтверждено, составляю план отчёта. Сообщу, когда он будет готов."
    )


def plan_failed(order_id: int, *, can_retry: bool) -> str:
    text = f"❌ Не удалось составить план отчёта по заказу #{order_id}."
    if can_retry:
        return f"{text} Нажмите «{RETRY_PLAN}» или отмените заказ."
    return f"{text} {PLAN_LIMIT_REACHED}"


def plan_already_restarted(order: Order) -> str:
    return f"План отчёта по заказу #{order.id} уже составляется."


def plan_confirmed(order_id: int) -> str:
    return f"✅ План отчёта по заказу #{order_id} подтверждён. Оплата пока недоступна."


def payment_pending(order_id: int) -> str:
    return (
        f"Заказ #{order_id}: план подтверждён. Оплата пока недоступна. "
        "Заказ можно отменить, чтобы оформить новый."
    )


def plan_view(order_id: int, plan: dict[str, object]) -> str:
    """План для пользователя: разделы, подразделы, объём — экранировано, ≤ 4096 символов.

    Длинный план сокращается по шагам PLAN_VIEW_LIMITS (длина проверяется уже
    после экранирования); последний шаг укладывается при любом допустимом плане.
    """
    text = ""
    for step, limits in enumerate(PLAN_VIEW_LIMITS):
        text = _render_plan(order_id, plan, limits, shortened=step > 0)
        if len(text) <= MAX_MESSAGE_CHARS:
            break
    return text


def _render_plan(
    order_id: int,
    plan: dict[str, object],
    limits: tuple[int, int, int, int, int, int],
    *,
    shortened: bool,
) -> str:
    title_len, desc_len, max_reqs, req_len, max_subs, sub_len = limits
    lines = [f"🗂 <b>План отчёта — заказ #{order_id}</b>"]
    total = plan.get("total_pages")
    if isinstance(total, int):
        lines.append(f"Объём: {total} стр.")
    sections = plan.get("sections")
    for number, section in enumerate(sections if isinstance(sections, list) else [], start=1):
        lines.append("")
        lines.append(
            f"<b>{number}. {_preview(str(section.get('title', '')), title_len)}</b>"
            f"{_pages_suffix(section.get('target_pages'))}"
        )
        description = str(section.get("description") or "")
        if desc_len and description:
            lines.append(_preview(description, desc_len))
        requirements = section.get("requirements") or []
        if max_reqs:
            lines += [f"• {_preview(str(item), req_len)}" for item in requirements[:max_reqs]]
        subsections = section.get("subsections") or []
        for sub_number, sub in enumerate(subsections[:max_subs], start=1):
            lines.append(
                f"   {number}.{sub_number}. {_preview(str(sub.get('title', '')), sub_len)}"
                f"{_pages_suffix(sub.get('target_pages'))}"
            )
        if len(subsections) > max_subs:
            lines.append(f"   подразделов: {len(subsections)}")
    lines.append("")
    if shortened:
        lines.append(PLAN_SHORTENED)
    lines.append(PLAN_NEXT_STEP)
    return "\n".join(lines)


def _pages_suffix(pages: object) -> str:
    return f" — {pages} стр." if isinstance(pages, int) and not isinstance(pages, bool) else ""


def requirements_summary(order_id: int, content: dict[str, object]) -> str:
    """Резюме ТЗ для подтверждения: без технических полей, данные экранированы.

    Длинные значения сокращаются по шагам SUMMARY_LIMITS, пока резюме не уложится
    в лимит сообщения Telegram (последний шаг укладывается при любых данных).
    """
    text = ""
    for value_limit, max_items in SUMMARY_LIMITS:
        text = _render_summary(order_id, content, value_limit, max_items)
        if len(text) <= MAX_MESSAGE_CHARS:
            break
    return text


def _render_summary(
    order_id: int, content: dict[str, object], value_limit: int, max_items: int
) -> str:
    lines = [f"📋 <b>ТЗ отчёта по практике — заказ #{order_id}</b>", ""]
    for name in REQUIRED_FIELDS:
        shown = _field_value(name, content.get(name), value_limit)
        lines.append(f"<b>{field_label(name)}:</b> {shown}")
    optional = [name for name in SUMMARY_OPTIONAL_FIELDS if content.get(name) not in (None, "")]
    raw_extra = content.get("extra_requirements")
    extra = (
        [item for item in raw_extra if isinstance(item, str)] if isinstance(raw_extra, list) else []
    )
    if optional or extra:
        lines += ["", "<i>Найдено в материалах:</i>"]
        for name in optional:
            shown = _field_value(name, content.get(name), value_limit)
            lines.append(f"<b>{field_label(name)}:</b> {shown}")
        if extra:
            lines.append(f"<b>{field_label('extra_requirements')}:</b>")
            lines += [f"• {_preview(item, value_limit)}" for item in extra[:max_items]]
            if len(extra) > max_items:
                lines.append(f"…и ещё {len(extra) - max_items}")
    lines += ["", REQUIREMENTS_CONFIRM_PROMPT]
    return "\n".join(lines)


def _field_value(field: str, value: object, limit: int) -> str:
    """Значение поля для пользователя: даты — ДД.ММ.ГГГГ, текст — сокращён и экранирован."""
    if value is None or value == "":
        return "—"
    if field in ("practice_start", "practice_end") and isinstance(value, str):
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    if field == "target_pages":
        return f"{value} стр."
    return _preview(str(value), limit)


def _preview(text: str, limit: int) -> str:
    # Сокращаем до экранирования, чтобы не разрезать HTML-сущность.
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return escape(text, quote=False)


def analysis_failed(order_id: int, *, can_retry: bool) -> str:
    text = f"❌ Не удалось проанализировать материалы по заказу #{order_id}."
    if can_retry:
        return f"{text} Нажмите «{RETRY_ANALYSIS}» или отмените заказ."
    return f"{text} {ANALYSIS_LIMIT_REACHED}"


def analysis_in_progress(order: Order) -> str:
    return f"Заказ #{order.id}: идёт анализ материалов. Сообщим, когда он завершится."


def analysis_already_restarted(order: Order) -> str:
    return f"Анализ материалов по заказу #{order.id} уже выполняется."


def materials_completed(completion: MaterialsCompletion) -> str:
    """Итог завершения приёма: счётчики и необработанные файлы, без технических деталей."""
    state = MaterialCompletionState
    lines = [
        f"📥 Материалы по заказу #{completion.order.id} приняты.",
        f"Файлов: {len(completion.materials)} · "
        f"текст извлечён: {completion.count(state.WITH_TEXT)} · "
        f"без текста: {completion.count(state.WITHOUT_TEXT)} · "
        f"с ошибкой: {completion.count(state.FAILED)}",
    ]
    unprocessed = [m for m in completion.materials if m.state is not state.WITH_TEXT]
    if unprocessed:
        lines += ["", "Не обработаны:"]
        for material in unprocessed:
            if material.state is state.FAILED and material.failure_reason is not None:
                reason = MATERIAL_FAILURE_TEXTS.get(
                    material.failure_reason, MATERIAL_PROCESSING_FAILED
                )
            elif material.state is state.FAILED:
                reason = MATERIAL_PROCESSING_FAILED
            else:
                reason = TEXT_NOT_FOUND
            lines.append(f"• «{file_label(material.original_filename)}» — {reason}")
    lines += ["", MATERIALS_NEXT_STEP]
    return "\n".join(lines)


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


def material_processing_result(outcome: ProcessingOutcome) -> str | None:
    """Сообщение о результате обработки материала; None — сообщать нечего.

    Без технических подробностей: причина сбоя и эвристики — только понятным текстом.
    """
    if not outcome.order_active:
        return None
    label = file_label(outcome.original_filename)
    match outcome.kind:
        case OutcomeKind.PROCESSED:
            assert outcome.result is not None
            return _text_extracted(label, outcome)
        case OutcomeKind.FAILED:
            return material_failed(label, _failure_text(outcome))
        case OutcomeKind.BUSY:
            return (
                f"⏳ Файл «{label}» ждёт обработки: очередь занята. "
                "Обработаем его автоматически и сообщим результат."
            )
        case _:
            return None


def _failure_text(outcome: ProcessingOutcome) -> str:
    if outcome.will_retry:
        return MATERIAL_RETRY_LATER
    assert outcome.failure_reason is not None
    return MATERIAL_FAILURE_TEXTS.get(outcome.failure_reason, MATERIAL_PROCESSING_FAILED)


def _text_extracted(label: str, outcome: ProcessingOutcome) -> str:
    result = outcome.result
    assert result is not None
    image_only = next((w for w in result.warnings if w.code is WarningCode.IMAGE_ONLY_PAGES), None)
    if result.has_warning(WarningCode.NO_TEXT):
        text = f"⚠️ Файл «{label}»: {TEXT_NOT_FOUND}"
        if image_only is not None:
            text += TEXT_LOOKS_SCANNED
        return text + TEXT_SEND_TEXT_VERSION

    text = f"📄 Файл «{label}»: текст извлечён"
    if result.page_count is not None:
        text += f" ({result.page_count} стр.)"
    text += "."
    for warning in result.warnings:
        if warning.code is WarningCode.PAGES_WITHOUT_TEXT:
            text += f" На страницах {format_pages(warning.pages)} текста нет"
            text += " — возможно, это сканы." if image_only is not None else "."
        elif warning.code is WarningCode.LOW_QUALITY_TEXT:
            text += TEXT_LOW_QUALITY
    match result.truncation:
        case TextTruncationReason.PAGE_LIMIT:
            text += (
                f" Обработаны только первые {result.pages_processed} страниц "
                f"из {result.page_count}."
            )
        case TextTruncationReason.CHAR_LIMIT:
            text += TEXT_CHAR_LIMIT
        case TextTruncationReason.TIME_BUDGET:
            text += TEXT_TIME_BUDGET
    return text


def format_pages(pages: tuple[int, ...]) -> str:
    """``(1, 2, 3, 5)`` -> ``"1–3, 5"``; длинный список обрезается."""
    ranges: list[str] = []
    start = prev = None
    for page in sorted(pages):
        if prev is not None and page == prev + 1:
            prev = page
            continue
        if start is not None:
            ranges.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = page
    if start is not None:
        ranges.append(str(start) if start == prev else f"{start}–{prev}")
    if len(ranges) > MAX_LISTED_PAGES:
        return ", ".join(ranges[:MAX_LISTED_PAGES]) + " и другие"
    return ", ".join(ranges)
