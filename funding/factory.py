import os, re, json, logging, hashlib, asyncio
from typing import List, Set, Optional, Union
from datetime import datetime
import json
import aiohttp
import secrets
import timeago
from quart import jsonify
from peewee import PostgresqlDatabase, SqliteDatabase, ProgrammingError
from playhouse.shortcuts import ReconnectMixin
from aiocryptocurrency.coins import Coin, SUPPORTED_COINS
from aiocryptocurrency import TransactionSet, Transaction

from aiocryptocurrency.coins.nero import Wownero, Monero
from quart import Quart, render_template, session, request, g
from quart_schema import RequestSchemaValidationError
from quart_session import Session
from quart_session_openid import OpenID

from funding.utils.discourse import Discourse
from funding.utils.globals import COINS_LOOKUP
from funding.utils.rates import Rates
import settings

class Firo(Coin):
    def __init__(self):
        super(Firo, self).__init__()
        self.host = '127.0.0.1'
        self.port = 8888
        self.basic_auth: Optional[tuple[str]] = None
        self.url = None

    async def send(self, address: str, amount: float) -> str:
        """returns txid"""
        if amount <= 0:
            raise Exception("amount cannot be zero or less")

        data = {
            "method": "sendtoaddress",
            "params": [address, amount]
        }

        blob = await self._make_request(data=data)
        return blob['result']

    async def mintspark(self, sparkAddress: str, amount: float, memo: str = "") -> str:
        """returns txid"""
        if amount <= 0:
            raise Exception("amount cannot be zero or less")

        params = {
            sparkAddress: {
                "amount": amount,
                "memo": memo
            }
        }

        data = {
            "method": "mintspark",
            "params": [params]
        }

        blob = await self._make_request(data=data)

        txids = blob['result']
        if isinstance(txids, list):
            txids = ', '.join(txids)

        return txids


    async def create_address(self) -> dict:
        """Returns both a transparent address and a Spark address."""

        address_data = {
            "method": "getnewaddress"
        }
        address_blob = await self._make_request(data=address_data)
        address = address_blob['result']

        if address is None or not isinstance(address, str):
            raise Exception("Invalid standard address result")

        return {
            "address": address,
        }

    async def check_address(self, addr_receiving: str) -> str:
        """Check address validation."""

        validation_data = {
            "method": "validateaddress",
            "params": [addr_receiving]
        }
        validation_blob = await self._make_request(data=validation_data)

        return validation_blob['result']

    async def tx_details(self, txid: str):
        if not isinstance(txid, str) or not txid:
            raise Exception("bad address")

        data = {
            "method": "gettransaction",
            "params": [txid]
        }

        blob = await self._make_request(data=data)
        return blob['result']

    async def list_txs(self, address: str = None, payment_id: str = None, minimum_confirmations: int = 3) -> Optional[TransactionSet]:
        txset = TransactionSet()
        if not isinstance(address, str) or not address:
            raise Exception("bad address")

        results = await self._make_request(data={
            "method": "listreceivedbyaddress",
            "params": [minimum_confirmations]
        })

        if not isinstance(results.get('result'), list):
            return txset

        try:
            result = [r for r in results['result'] if r['address'] == address][0]
        except Exception as ex:
            return txset

        for txid in result.get('txids', []):
            # fetch tx details
            tx = await self.tx_details(txid)

            # fetch blockheight
            tx['blockheight'] = await self.blockheight(tx['blockhash'])
            date = datetime.fromtimestamp(tx['blocktime'])

            txset.add(Transaction(amount=tx['amount'],
                                  txid=tx['txid'],
                                  date=date,
                                  blockheight=tx['blockheight'],
                                  direction='in',
                                  confirmations=tx['confirmations']))

        return txset

    async def blockheight(self, blockhash: str) -> int:
        """blockhash -> blockheight"""
        if not isinstance(blockhash, str) or not blockhash:
            raise Exception("bad address")

        data = {
            "method": "getblock",
            "params": [blockhash]
        }
        blob = await self._make_request(data=data)

        height = blob['result'].get('height', 0)
        return height

    async def _generate_url(self) -> None:
        self.url = f'http://{self.host}:{self.port}/'

    async def _make_request(self, data: dict = None) -> dict:
        await self._generate_url()

        opts = {
            "headers": {
                "User-Agent": self.user_agent
            }
        }

        if self.basic_auth:
            opts['auth'] = await self._make_basic_auth()

        async with aiohttp.ClientSession(**opts) as session:
            async with session.post(self.url, json=data) as resp:
                if resp.status == 401:
                    raise Exception("Unauthorized")
                blob = await resp.json()
                if 'result' not in blob:
                    if blob:
                        blob = json.dumps(blob, indent=4, sort_keys=True)
                    raise Exception(f"Invalid response: {blob}")
                return blob

cache = None
peewee = None
rates = Rates()
app: Optional[Quart] = None
openid: Optional[OpenID] = None
crypto_provider: Optional[Firo] = Firo()
coin: Optional[dict] = None
discourse = Discourse()
proposal_task = None


class ReconnectingPGDatabase(ReconnectMixin, PostgresqlDatabase):
    def __init__(self, *args, **kwargs):
        import peewee as pw
        super(ReconnectingPGDatabase, self).__init__(*args, **kwargs)
        self._reconnect_errors = {
            pw.InterfaceError: ["connection already closed"]
        }


database = ReconnectingPGDatabase(
    settings.DB_NAME,
    autorollback=True,
    user=settings.DB_USER,
    password=settings.DB_PASSWD,
    host=settings.DB_HOST,
    port=settings.DB_PORT
)

async def _setup_postgres(app: Quart):
    import peewee
    import funding.models.database
    models = peewee.Model.__subclasses__()
    for m in models:
        m.create_table()


async def _setup_openid(app: Quart):
    global openid
    if settings.OPENID_CFG:
        openid = OpenID(app, **settings.OPENID_CFG)


async def _setup_settings(app: Quart):
    global coin
    # simple configuration validation
    coin_names = ",".join(SUPPORTED_COINS.keys())
    if not settings.COIN_NAME:
        raise Exception("Please specify a coin name")

    ttf = settings.CAPTCHA_TTF
    if not os.path.exists(ttf):
        raise Exception(f"Font not found: {ttf}")

    if settings.COIN_NAME not in SUPPORTED_COINS:
        raise Exception(f"{settings.COIN_NAME} is not supported. "
                        f"Accepted names: {coin_names}")
    coin = COINS_LOOKUP[settings.COIN_NAME]

    if settings.COIN_RPC_AUTH:
        if ',' not in settings.COIN_RPC_AUTH:
            raise Exception("COIN_RPC_AUTH needs to be a tuple delimited by: ,")
        settings.COIN_RPC_AUTH = settings.COIN_RPC_AUTH.split(",", 1)

    if not settings.DOMAIN:
        raise Exception(
            "Please set variable 'DOMAIN' to the domain where this "
            "web application will run under. e.g: \"funding.domain.org\"")
    app.config['SERVER_NAME'] = settings.DOMAIN

    if settings.DISCOURSE_ENABLED:
        if not settings.DISCOURSE_DOMAIN:
            raise Exception("DISCOURSE_DOMAIN not set. e.g: forum.domain.org")
        if not settings.DISCOURSE_API_KEY:
            raise Exception("DISCOURSE_API_KEY not set.")
        settings.DISCOURSE_DOMAIN = settings.DISCOURSE_DOMAIN.replace("/", "")

    if getattr(settings, "MAIL_ENABLED", False):
        if not getattr(settings, "MAIL_FROM", ""):
            raise Exception("MAIL_FROM not set. e.g: noreply@mg.domain.org")
        if not getattr(settings, "SMTP_HOST", ""):
            raise Exception("SMTP_HOST not set. e.g: smtp.mailgun.org")

    if isinstance(settings.OPENID_CFG, dict):
        for needle in ['client_id', 'client_secret', 'configuration']:
            if needle not in settings.OPENID_CFG:
                raise Exception(f"missing key '{needle}' in OPENID_CFG")


async def _setup_crypto(app: Quart):
    global crypto_provider
    providers = {
        "wownero": Wownero,
        "monero": Monero,
        "firo": Firo
    }

    crypto_provider = providers[settings.COIN_NAME]()
    crypto_provider.port = settings.COIN_RPC_PORT
    if settings.COIN_RPC_AUTH:
        crypto_provider.basic_auth = settings.COIN_RPC_AUTH

async def _setup_cache(app: Quart):
    global cache
    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_URI'] = settings.REDIS_URI
    Session(app)


async def _setup_tasks(app: Quart):
    """Schedules a series of tasks at an interval."""
    asyncio.create_task(discourse.fetch_task())
    asyncio.create_task(rates.rate_task())
    from funding.proposals.task import ProposalFundedTask
    asyncio.create_task(ProposalFundedTask().is_funded_task())


async def _setup_error_handlers(app: Quart):
    @app.errorhandler(500)
    async def page_error(e):
        return await render_template('error.html', code=500, msg="Error occurred"), 500

    @app.errorhandler(403)
    async def page_forbidden(e):
        return await render_template('error.html', code=403, msg="Forbidden"), 403

    @app.errorhandler(404)
    async def page_not_found(e):
        return await render_template('error.html', code=404, msg="Page not found"), 404


def create_app():
    global app
    app = Quart(__name__)
    app.logger.setLevel(logging.INFO)
    app.secret_key = settings.APP_SECRET

    @app.template_filter()
    def to_usd(val: float):
        global rates
        return rates.to_usd(val)

    @app.template_filter()
    def dt_human(val):
        return val.strftime('%Y-%m-%d %H:%M')

    @app.template_filter()
    def dt_ago(val):
        val = val.replace(tzinfo=None)
        return timeago.format(val, datetime.now())

    @app.template_filter()
    def hash_md5(val):
        if not val:
            return ""
        return hashlib.md5(val.encode()).hexdigest()

    @app.context_processor
    def template_variables():
        global openid
        from funding.models.database import User, UserRole
        from funding.utils.globals import GLOBALS
        now = datetime.now()

        data = {
            'settings': settings,
            'path': request.path,
            'coin': COINS_LOOKUP[settings.COIN_NAME],
            'rates': rates,
            'UserRole': UserRole,
            'user': g.user,
            'logged_in': g.user.is_anon is not True,
            **GLOBALS
        }

        if settings.OPENID_CFG:
            data['url_login'] = openid.endpoint_name_login

        if g.user.is_admin:
            data['csrf_token'] = session.setdefault('csrf_token', secrets.token_urlsafe(32))

        return dict(year=now.year, **data)

    @app.errorhandler(RequestSchemaValidationError)
    async def handle_request_validation_error(error):
        return jsonify({"errors": str(error)}), 400

    @app.before_serving
    async def startup():
        await _setup_error_handlers(app)
        await _setup_settings(app)
        await _setup_crypto(app)
        await _setup_cache(app)
        await _setup_openid(app)
        await _setup_postgres(app)
        await _setup_tasks(app)

        from funding.routes import bp_routes
        from funding.auth.routes import bp_auth
        from funding.proposals.routes import bp_proposals
        from funding.proposals.api import bp_proposals_api
        app.register_blueprint(bp_routes)
        app.register_blueprint(bp_auth)
        app.register_blueprint(bp_proposals)
        app.register_blueprint(bp_proposals_api)

        app.jinja_env.trim_blocks = True
        app.jinja_env.lstrip_blocks = True

        from funding.utils import crumbs

    @app.before_request
    async def set_request_ctx():
        from funding.models.database import User, UserRole
        g.user = User()
        g.user.role = UserRole.anonymous

        if '/static/' in request.url:
            return

        ses = session.get('user')
        if not isinstance(ses, dict):
            return

        try:
            user = User.by_uuid(ses['uuid'])
            if not user or not user.enabled:
                raise Exception("clear session")
            if not settings.OPENID_CFG and ses.get('password_version') != user.password_version:
                raise Exception("clear session")
            g.user = user
        except:
            session.clear()

    return app
