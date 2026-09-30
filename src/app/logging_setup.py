import logging

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    # Логи aiogram о каждом апдейте слишком шумные для INFO.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
