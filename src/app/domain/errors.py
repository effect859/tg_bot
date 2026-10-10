"""Доменные ошибки заказа. Сообщения — для логов и разработчика, не для пользователя."""

from enum import StrEnum

from app.domain.enums import MaterialProcessingStatus, OrderStatus


class OrderDomainError(Exception):
    """Базовая ошибка доменной модели заказа."""


class OrderNotFoundError(OrderDomainError):
    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id} not found")
        self.order_id = order_id


class ActiveOrderExistsError(OrderDomainError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"User {user_id} already has an active order")
        self.user_id = user_id


class InvalidStatusTransitionError(OrderDomainError):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"Transition {current} -> {target} is not allowed")
        self.current = current
        self.target = target


class OrderNotEditableError(OrderDomainError):
    """ТЗ и план нельзя менять в текущем статусе заказа (например, после оплаты)."""

    def __init__(self, status: OrderStatus) -> None:
        super().__init__(f"Requirements and plan cannot be changed in status {status}")
        self.status = status


class RequirementsNotConfirmedError(OrderDomainError):
    """Нет подтверждённой актуальной версии ТЗ."""


class PlanNotConfirmedError(OrderDomainError):
    """Нет подтверждённой актуальной версии плана."""


class OrderNotPaidError(OrderDomainError):
    """PAID без списания кредитов за заказ (строки ORDER_CHARGE) недопустим."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id} has no credit charge")
        self.order_id = order_id


class OrderNotRefundedError(InvalidStatusTransitionError):
    """Отмена оплаченного заказа без возврата кредитов (строки ORDER_REFUND) недопустима.

    Подкласс InvalidStatusTransitionError: для хендлеров это обычный отказ в переходе
    («действие неактуально»), например у старой кнопки «Отменить» после сбоя генерации.
    """

    def __init__(self, order_id: int) -> None:
        super().__init__(OrderStatus.FAILED, OrderStatus.CANCELLED)
        self.order_id = order_id


class DraftVersionExistsError(OrderDomainError):
    """У заказа уже есть черновик версии: его нужно подтвердить или отредактировать."""


class VersionNotFoundError(OrderDomainError):
    """Версия не найдена или относится к другому заказу."""


class VersionAlreadyConfirmedError(OrderDomainError):
    """Подтверждённая версия неизменна; изменения — только через новую версию."""


class OutdatedVersionError(OrderDomainError):
    """Версия устарела: после неё уже создана более новая."""


class ConsentNotGivenError(OrderDomainError):
    """Материалы принимаются только после согласия пользователя."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id}: consent has not been given")
        self.order_id = order_id


class MaterialsNotAcceptedError(OrderDomainError):
    """Заказ в статусе, в котором материалы не принимаются."""

    def __init__(self, status: OrderStatus) -> None:
        super().__init__(f"Materials cannot be added in status {status}")
        self.status = status


class MaterialsLockedError(OrderDomainError):
    """ТЗ подтверждено: набор материалов зафиксирован."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id}: requirements confirmed, materials are locked")
        self.order_id = order_id


class MaterialLimitError(OrderDomainError):
    def __init__(self, order_id: int, limit: int) -> None:
        super().__init__(f"Order {order_id}: material limit of {limit} reached")
        self.order_id = order_id
        self.limit = limit


class MaterialRejectReason(StrEnum):
    """Почему файл не принят: по причине бот выбирает понятное пользователю сообщение."""

    FILENAME = "filename"
    FORMAT = "format"
    MIME = "mime"
    EMPTY = "empty"
    # Больше MAX_MATERIAL_SIZE_BYTES.
    SIZE = "size"
    # Содержимое не соответствует расширению (сигнатура, структура, кодировка).
    CONTENT = "content"


class UnsupportedMaterialError(OrderDomainError):
    """Файл не принимается: формат, размер, метаданные или содержимое не проходят проверку."""

    def __init__(self, message: str, *, reason: MaterialRejectReason) -> None:
        super().__init__(message)
        self.reason = reason


class MaterialsNotReadyReason(StrEnum):
    """Почему приём материалов нельзя завершить: по причине бот выбирает сообщение."""

    NO_MATERIALS = "no_materials"
    # Есть материалы, обработка которых ещё не завершена (в том числе ждущие повтора).
    PENDING = "pending"
    # Обработка завершена, но ни из одного материала не получен непустой текст.
    NO_TEXT = "no_text"


class MaterialsNotReadyError(OrderDomainError):
    """Материалы заказа не готовы: перейти к анализу нельзя."""

    def __init__(self, reason: MaterialsNotReadyReason, *, pending: int = 0) -> None:
        super().__init__(f"Materials are not ready: {reason}, pending={pending}")
        self.reason = reason
        self.pending = pending


class AnalysisLimitReachedError(OrderDomainError):
    """Исчерпан лимит вызовов модели на анализ материалов заказа."""

    def __init__(self, order_id: int, limit: int) -> None:
        super().__init__(f"Order {order_id}: analysis LLM call limit of {limit} reached")
        self.order_id = order_id
        self.limit = limit


class PlanLimitReachedError(OrderDomainError):
    """Исчерпан лимит вызовов модели на составление плана заказа."""

    def __init__(self, order_id: int, limit: int) -> None:
        super().__init__(f"Order {order_id}: plan LLM call limit of {limit} reached")
        self.order_id = order_id
        self.limit = limit


class PlanTargetPagesUnsupportedError(OrderDomainError):
    """Объём подтверждённого ТЗ вне поддерживаемого диапазона: повтор плана бесполезен."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id}: requirements target pages are not supported")
        self.order_id = order_id


class RequirementFieldNotEditableError(OrderDomainError):
    """Поле ТЗ нельзя изменить из диалога: редактируются только обязательные поля."""

    def __init__(self, field: str) -> None:
        super().__init__(f"Requirement field {field!r} is not editable")
        self.field = field


class RequirementsChangedError(OrderDomainError):
    """ТЗ изменилось после того, как пользователь его увидел: подтверждать нечего."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id}: requirements changed since they were shown")
        self.order_id = order_id


class RequirementsVolumeConsentError(OrderDomainError):
    """Кнопка не соответствует объёму ТЗ: согласие на работу без объёма нужно именно
    при неподдерживаемом объёме и только при нём."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id}: volume consent does not match requirements")
        self.order_id = order_id


class PlanChangedError(OrderDomainError):
    """План изменился после того, как пользователь его увидел: подтверждать нечего."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id}: plan changed since it was shown")
        self.order_id = order_id


class PlanSectionTooLargeError(OrderDomainError):
    """В плане раздел больше, чем генератор пишет одним ответом: план не подтверждается."""

    def __init__(self, order_id: int, sections: list[int]) -> None:
        super().__init__(f"Order {order_id}: plan sections {sections} exceed generator limit")
        self.order_id = order_id
        self.sections = sections


class MaterialNotFoundError(OrderDomainError):
    def __init__(self, material_id: int) -> None:
        super().__init__(f"Material {material_id} not found")
        self.material_id = material_id


class InvalidMaterialTransitionError(OrderDomainError):
    def __init__(self, current: MaterialProcessingStatus, target: MaterialProcessingStatus) -> None:
        super().__init__(f"Material transition {current} -> {target} is not allowed")
        self.current = current
        self.target = target
