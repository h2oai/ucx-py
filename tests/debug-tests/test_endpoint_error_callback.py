# This test requires InfiniBand, to run:
# UCXPY_IFNAME=ib0 UCX_NET_DEVICES=mlx5_0:1 UCX_TLS=rc,tcp,sockcm,cuda_copy UCX_SOCKADDR_TLS_PRIORITY=sockcm py.test --cache-clear tests/debug-tests/test_endpoint_error_callback.py

import asyncio
import logging
import multiprocessing
import os
import queue
import random
import signal
import sys

from distributed.comm.utils import to_frames
from distributed.protocol import to_serialize

import cloudpickle
import pytest
import ucp
from ucp.utils import get_ucxpy_logger
from utils import get_cuda_devices, get_num_gpus, recv, send

cupy = pytest.importorskip("cupy")


def get_log_queue_handler():
    handler = logging.StreamHandler()

    log_queue = queue.Queue(-1)  # no limit on size
    queue_handler = logging.handlers.QueueHandler(log_queue)
    listener = logging.handlers.QueueListener(log_queue, handler)

    ucxpy_logger = get_ucxpy_logger()
    ucxpy_logger.addHandler(queue_handler)

    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)

    return log_queue, listener


async def get_ep(name, port):
    addr = ucp.get_address()
    ep = await ucp.create_endpoint(addr, port)
    return ep


def client(port, func):
    # wait for server to come up
    # receive object
    # process suicides

    ucp.init()

    # must create context before importing
    # cudf/cupy/etc

    async def read():
        await asyncio.sleep(1)
        ep = await get_ep("client", port)
        msg = None
        import cupy

        cupy.cuda.set_allocator(None)

        frames, msg = recv(ep)

        # Client process suicides to force an "Endpoint timeout"
        # on the server
        os.kill(os.getpid(), signal.SIGKILL)

    asyncio.get_event_loop().run_until_complete(read())


def server(port, func):
    # create listener receiver
    # add queue logger
    # write cudf object
    # terminates ep/listener
    # checks that "Endpoint timeout" was logged
    ucp.init()

    log_queue, log_listener = get_log_queue_handler()
    log_listener.start()

    async def f(listener_port):
        # coroutine shows up when the client asks
        # to connect
        async def write(ep):
            import cupy

            cupy.cuda.set_allocator(None)

            print("CREATING CUDA OBJECT IN SERVER...")
            cuda_obj_generator = cloudpickle.loads(func)
            cuda_obj = cuda_obj_generator()
            msg = {"data": to_serialize(cuda_obj)}
            frames = await to_frames(msg, serializers=("cuda", "dask", "pickle"))

            # Send meta data
            try:
                await send(ep, frames)
            except Exception:
                # Avoids process hanging on "Endpoint timeout"
                pass

            print("Shutting Down Server...")
            await ep.close()
            lf.close()

        lf = ucp.create_listener(write, port=listener_port)
        try:
            while not lf.closed():
                await asyncio.sleep(0.1)
        except ucp.UCXCloseError:
            pass

    log_listener.stop()

    asyncio.get_event_loop().run_until_complete(f(port))

    # Check log for the expected "Endpoint timeout" and exits with
    # status 0 if encountered, -1 otherwise.
    while not log_queue.empty():
        log = log_queue.get()
        print(log)
        if "Endpoint timeout" in log.getMessage():
            sys.exit(0)
    sys.exit(-1)


def cupy_obj():
    import cupy

    size = 10 ** 8
    return cupy.arange(size)


@pytest.mark.skipif(
    get_num_gpus() <= 2, reason="Machine does not have more than two GPUs"
)
def test_send_recv_cu():
    base_env = os.environ
    env_client = base_env.copy()
    # grab first two devices
    cvd = get_cuda_devices()[:2]
    cvd = ",".join(map(str, cvd))
    # reverse CVD for other worker
    env_client["CUDA_VISIBLE_DEVICES"] = cvd[::-1]

    port = random.randint(13000, 15500)
    # serialize function and send to the client and server
    # server will use the return value of the contents,
    # serialize the values, then send serialized values to client.
    # client will compare return values of the deserialized
    # data sent from the server

    func = cloudpickle.dumps(cupy_obj)
    ctx = multiprocessing.get_context("spawn")
    server_process = ctx.Process(name="server", target=server, args=[port, func])
    client_process = ctx.Process(name="client", target=client, args=[port, func])

    server_process.start()
    # cudf will ping the driver for validity of device
    # this will influence device on which a cuda context is created.
    # work around is to update env with new CVD before spawning
    os.environ.update(env_client)
    client_process.start()

    server_process.join()
    client_process.join()

    assert server_process.exitcode == 0
    assert client_process.exitcode == 1
