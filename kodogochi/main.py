"""Точка входа: uvicorn kodogochi.main:app"""
import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

app = create_app()
