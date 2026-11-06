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
                assert {'warranty_queue_0','ops_notifications_0'} <= buttons
                assert not {'ops_providers_0','ops_performance'} & buttons
                from models.product import Product
                from handlers.admin_product_manage import _build_product_panel
                product = Product(id=21, name='Fixture product', price=Decimal('1'), stock=2,
                                  delivery_type='automatic', manual_fallback=False, warranty_days=7)
                panel = _build_product_panel(product)
                buttons = {b.callback_data for row in panel[1].inline_keyboard for b in row}
                assert 'warranty_config_21' in buttons
            # Real ORM money writes and ledger rows must commit / roll back together.
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from models.user import User
            from models.customer_tools import WalletEntry
            fixture_engine=create_engine('sqlite:///:memory:')
            User.__table__.create(fixture_engine)
            WalletEntry.__table__.create(fixture_engine)
            factory=sessionmaker(bind=fixture_engine,expire_on_commit=False)
            with factory.begin() as db:
                db.add(User(telegram_id=7,full_name='Fixture',balance=Decimal('10')))
            with factory.begin() as db:
                user=db.query(User).filter_by(telegram_id=7).one()
                user.balance=Decimal('8')
                user.total_spent=Decimal('2')
            with factory() as db:
                entries=db.query(WalletEntry).order_by(WalletEntry.id).all()
                assert [(x.amount,x.balance) for x in entries]==[(Decimal('10'),Decimal('10')),(Decimal('-2'),Decimal('8'))]
            try:
                with factory.begin() as db:
                    user=db.query(User).filter_by(telegram_id=7).one()
                    user.balance=Decimal('5')
                    db.flush()
                    raise ValueError('rollback fixture')
            except ValueError: pass
            with factory() as db:
                assert db.query(User).one().balance==Decimal('8')
                assert db.query(WalletEntry).count()==2
            fixture_engine.dispose()
            asyncio.run(bot_app.bot.session.close())
        ''')
        result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
