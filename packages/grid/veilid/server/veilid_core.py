# stdlib
import base64
from collections.abc import Callable
import json
import lzma

# third party
import httpx
from loguru import logger
import veilid
from veilid import KeyPair
from veilid import Sequencing
from veilid import Stability
from veilid import TypedKey
from veilid import ValueData
from veilid import VeilidUpdate
from veilid.json_api import _JsonRoutingContext
from veilid.json_api import _JsonVeilidAPI
from veilid.types import RouteId

# relative
from .constants import HOST
from .constants import PORT
from .constants import USE_DIRECT_CONNECTION
from .veilid_db import load_dht_key
from .veilid_db import store_dht_key
from .veilid_db import store_dht_key_creds


async def main_callback(update: VeilidUpdate) -> None:
    # TODO: Handle other types of network events like
    # when our private route goes
    if update.kind == veilid.VeilidUpdateKind.APP_MESSAGE:
        logger.info(f"Received App Message: {update.detail.message}")

    elif update.kind == veilid.VeilidUpdateKind.APP_CALL:
        logger.info(f"Received App Call: {update.detail.message}")
        message: dict = json.loads(update.detail.message)

        async with httpx.AsyncClient() as client:
            data = message.get("data", None)
            # TODO: can we optimize this?
            # We encode the data to base64,as while sending
            # json expects valid utf-8 strings
            if data:
                message["data"] = base64.b64decode(data)
            response = await client.request(
                method=message.get("method"),
                url=message.get("url"),
                data=message.get("data", None),
                params=message.get("params", None),
                json=message.get("json", None),
            )

        async with await get_veilid_conn() as conn:
            compressed_response = lzma.compress(response.content)
            logger.info(f"Compression response size: {len(compressed_response)}")
            await conn.app_call_reply(update.detail.call_id, compressed_response)


async def noop_callback(update: VeilidUpdate) -> None:
    pass


async def get_veilid_conn(
    host: str = HOST, port: int = PORT, update_callback: Callable = noop_callback
) -> _JsonVeilidAPI:
    return await veilid.json_api_connect(
        host=host, port=port, update_callback=update_callback
    )


async def get_routing_context(conn: _JsonVeilidAPI) -> _JsonRoutingContext:
    if USE_DIRECT_CONNECTION:
        return await (await conn.new_routing_context()).with_safety(
            veilid.SafetySelection.unsafe(veilid.Sequencing.ENSURE_ORDERED)
        )
    else:
        return await (await conn.new_routing_context()).with_sequencing(
            veilid.Sequencing.ENSURE_ORDERED
        )


class VeilidConnectionSingleton:
    _instance = None

    def __new__(cls) -> "VeilidConnectionSingleton":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._connection = None
        return cls._instance

    def __init__(self) -> None:
        self._connection: _JsonVeilidAPI | None = None

    @property
    def connection(self) -> _JsonVeilidAPI | None:
        return self._connection

    async def initialize_connection(self) -> None:
        if self._connection is None:
            self._connection = await get_veilid_conn(update_callback=main_callback)
            logger.info("Connected to Veilid")

    async def release_connection(self) -> None:
        if self._connection is not None:
            await self._connection.release()
            logger.info("Disconnected  from Veilid")
            self._connection = None


async def create_private_route(
    conn: _JsonVeilidAPI,
    stability: Stability = veilid.Stability.RELIABLE,
    sequencing: Sequencing = veilid.Sequencing.ENSURE_ORDERED,
) -> tuple[RouteId, bytes]:
    route_id, route_blob = await conn.new_custom_private_route(
        [veilid.CryptoKind.CRYPTO_KIND_VLD0],
        stability=stability,
        sequencing=sequencing,
    )
    logger.info(f"Private Route created with Route ID: {route_id}")
    return (route_id, route_blob)


async def get_node_id(conn: _JsonVeilidAPI) -> str:
    state = await conn.get_state()
    config = state.config.config
    node_id = config.network.routing_table.node_id[0]
    return node_id


async def generate_dht_key() -> str:
    logger.info("Generating DHT Key")

    async with await get_veilid_conn() as conn:
        if await load_dht_key(conn):
            return "DHT Key already exists"

        async with await get_routing_context(conn) as router:
            dht_record = await router.create_dht_record(veilid.DHTSchema.dflt(1))

            if USE_DIRECT_CONNECTION:
                node_id = await get_node_id(conn)
                await router.set_dht_value(dht_record.key, 0, node_id.encode())
            else:
                _, route_blob = await create_private_route(conn)
                await router.set_dht_value(dht_record.key, 0, route_blob)

            await router.close_dht_record(dht_record.key)

            keypair = KeyPair.from_parts(
                key=dht_record.owner, secret=dht_record.owner_secret
            )

            await store_dht_key(conn, dht_record.key)
            await store_dht_key_creds(conn, keypair)

    return "DHT Key generated successfully"


async def retrieve_dht_key() -> str:
    async with await get_veilid_conn() as conn:
        dht_key = await load_dht_key(conn)

        if dht_key is None:
            raise Exception("DHT Key does not exist. Please generate one.")
        return str(dht_key)


async def get_dht_value(
    router: _JsonRoutingContext,
    dht_key: TypedKey,
    subkey: int,
    force_refresh: bool = True,
) -> ValueData:
    try:
        await router.open_dht_record(key=dht_key, writer=None)
    except Exception as e:
        raise Exception(f"Unable to open DHT Record:{dht_key} . Exception: {e}")

    try:
        dht_value = await router.get_dht_value(
            key=dht_key, subkey=subkey, force_refresh=force_refresh
        )
        # NOTE: Always close the DHT record after reading the value
        await router.close_dht_record(dht_key)
        return dht_value
    except Exception as e:
        raise Exception(
            f"Unable to get subkey value:{subkey} from DHT Record:{dht_key}. Exception: {e}"
        )


# TODO: change verbosity of logs to debug at appropriate places
async def get_route_from_dht_record(
    dht_key: str, conn: _JsonVeilidAPI, router: _JsonRoutingContext
) -> str | RouteId:
    dht_key = veilid.TypedKey(dht_key)
    logger.info(f"App Call to DHT Key: {dht_key}")
    dht_value = await get_dht_value(router, dht_key, 0)
    logger.info(f"DHT Value:{dht_value}")

    if USE_DIRECT_CONNECTION:
        route = dht_value.data.decode()
        logger.info(f"Node ID: {route}")
    else:
        route = await conn.import_remote_private_route(dht_value.data)
        logger.info(f"Private Route of  Peer: {route} ")

    return route


async def app_message(dht_key: str, message: bytes) -> str:
    async with await get_veilid_conn() as conn:
        async with await get_routing_context(conn) as router:
            route = await get_route_from_dht_record(dht_key, conn, router)

            await router.app_message(route, message)

            return "Message sent successfully"


async def app_call(dht_key: str, message: bytes) -> bytes:
    async with await get_veilid_conn() as conn:
        async with await get_routing_context(conn) as router:
            route = await get_route_from_dht_record(dht_key, conn, router)

            result = await router.app_call(route, message)

            return result


async def healthcheck() -> bool:
    async with await get_veilid_conn() as conn:
        state = await conn.get_state()
        return state.network.started