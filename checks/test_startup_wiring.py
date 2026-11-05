"""Import the real app in a fresh process with dummy config and network blocked."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class StartupWiringTests(unittest.TestCase):
    def test_real_app_imports_and_registers_feature_menus(self):
        root = Path(__file__).resolve().parents[1]
        # Do not inherit production credentials or read any dotenv files.
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'}
        env = {k:v for k,v in os.environ.items() if k.upper() in allowed}
        env.update(BOT_TOKEN='123456:'+'A'*35, MYSQL_HOST='127.0.0.1', MYSQL_USER='fixture',
                   MYSQL_PASSWORD='fixture', MYSQL_DB='fixture', MYSQL_PORT='3306', PYTHONIOENCODING='utf-8')
        script = textwrap.dedent('''
            import asyncio
            import contextlib
            import io
            import logging
            from decimal import Decimal
            from unittest.mock import patch
            import dotenv
            import sqlalchemy.engine

            def denied(*args, **kwargs):
                raise AssertionError('Startup import attempted a network/database connection')

            logging.disable(logging.CRITICAL)
            with patch.object(dotenv, 'load_dotenv', return_value=False), \
                 patch.object(sqlalchemy.engine.Engine, 'connect', denied), \
                 patch('socket.socket.connect', denied), \
                 contextlib.redirect_stdout(io.StringIO()):
                import bot_app
                import render_api
                import main
                from sqlalchemy.orm import configure_mappers
                configure_mappers()
                from database import engine
                assert engine.hide_parameters
                from handlers.bot_operations import router
                assert router in bot_app.dp.sub_routers
                from handlers.admin import _get_admin_panel_with_providers
                buttons = {b.callback_data for row in _get_admin_panel_with_providers().inline_keyboard for b in row}
                assert {'warranty_queue_0','ops_providers_0','ops_notifications_0','ops_performance'} <= buttons
                from models.product import Product
                from handlers.admin_product_manage import _build_product_panel
                product = Product(id=21, name='Fixture product', price=Decimal('1'), stock=2,
                                  delivery_type='automatic', manual_fallback=False, warranty_days=7)
                panel = _build_product_panel(product)
                buttons = {b.callback_data for row in panel[1].inline_keyboard for b in row}
                assert 'warranty_config_21' in buttons
            asyncio.run(bot_app.bot.session.close())
        ''')
        result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
