"""Доменные ошибки заказа. Сообщения — для логов и разработчика, не для пользователя."""

from app.domain.enums import OrderStatus


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


class DraftVersionExistsError(OrderDomainError):
    """У заказа уже есть черновик версии: его нужно подтвердить или отредактировать."""


class VersionNotFoundError(OrderDomainError):
    """Версия не найдена или относится к другому заказу."""


class VersionAlreadyConfirmedError(OrderDomainError):
    """Подтверждённая версия неизменна; изменения — только через новую версию."""


class OutdatedVersionError(OrderDomainError):
    """Версия устарела: после неё уже создана более новая."""
