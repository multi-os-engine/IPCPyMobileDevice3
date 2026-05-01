# Copyright (C) 2025 Berstanio
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import json
import os
import platform
import sys
import traceback
from pathlib import Path

import asyncio
import plistlib
import aiohttp
import pymobiledevice3.usbmux as usbmux
from packaging.version import Version
from pymobiledevice3.exceptions import *
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.common import TunnelProtocol
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.mobile_image_mounter import auto_mount
from pymobiledevice3.tcp_forwarder import LockdownTcpForwarder, UsbmuxTcpForwarder, TcpForwarderBase
from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid, TUNNELD_DEFAULT_ADDRESS
from pymobiledevice3.tunneld.server import TunneldRunner
from pymobiledevice3.usbmux import *

IPC_VERSION = 6

# Global event used to signal graceful shutdown from the "exit" command.
_shutdown_event: asyncio.Event | None = None

class TcpForwarderResource:

    def __init__(self, forwarder: TcpForwarderBase, task: asyncio.Task):
        self.forwarder = forwarder
        self.task = task

    async def close(self):
        self.forwarder.stop()
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except asyncio.TimeoutError:
            print(f"Joining forwarder task {self.forwarder.src_port} timed out")
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

class IPCClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, address, version):
        self.reader = reader
        self.writer = writer
        self.address = address
        self.version = version
        self.active_forwarder: dict[int, TcpForwarderResource] = {}
        self._write_lock = asyncio.Lock()
        self._command_tasks: set[asyncio.Task] = set()

    def track_task(self, task: asyncio.Task):
        """Register a command task so it can be cleaned up on disconnect."""
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

    async def close(self):
        # Cancel all in-flight command tasks and wait for them to finish.
        for task in self._command_tasks:
            task.cancel()
        if self._command_tasks:
            await asyncio.gather(*self._command_tasks, return_exceptions=True)
        self._command_tasks.clear()

        for resource in self.active_forwarder.values():
            await resource.close()

        self.active_forwarder.clear()

        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass

    def add_forwarder(self, local_port: int, forwarder: TcpForwarderResource):
        if local_port in self.active_forwarder:
            raise ValueError(f"{local_port} already has an open connection: {self.active_forwarder[local_port]}")
        self.active_forwarder[local_port] = forwarder

    async def close_forwarder(self, local_port: int):
        if local_port not in self.active_forwarder:
            raise ValueError(f"Port {local_port} is not open")
        forwarder = self.active_forwarder.pop(local_port)
        await forwarder.close()

    async def write_reply(self, reply: dict):
        message = json.dumps(reply)
        print(f"{self.address}: Sending packet: {message}")
        async with self._write_lock:
            self.writer.write(message.encode() + b"\n")
            await self.writer.drain()


async def list_devices(id, ipc_client):
    devices = []
    for device in await usbmux.list_devices():
        udid = device.serial
        async with await create_using_usbmux(udid, autopair=False, connection_type=device.connection_type) as lockdown:
            devices.append(lockdown.short_info)

    await ipc_client.write_reply({"id": id, "state": "completed", "result": devices})


async def list_devices_udid(id, ipc_client):
    devices = []
    for device in await usbmux.list_devices():
        devices.append(device.serial)

    await ipc_client.write_reply({"id": id, "state": "completed", "result": devices})


async def get_device(id, device_id, ipc_client):
    try:
        async with await create_using_usbmux(device_id, autopair=False) as lockdown:
            await ipc_client.write_reply({"id": id, "state": "completed", "result": lockdown.short_info})
    except (NoDeviceConnectedError, DeviceNotFoundError):
        await ipc_client.write_reply({"id": id, "state": "failed_no_device"})


async def install_app(id, lockdown_client, path, mode, ipc_client):
    async with InstallationProxyService(lockdown=lockdown_client) as installer:
        options = {"PackageType": "Developer"}

        def progress_handler(progress, *args):
            asyncio.ensure_future(ipc_client.write_reply({"id": id, "state": "progress", "progress": progress}))

        if mode == "INSTALL":
            await installer.install_from_local(path, cmd="Install", options=options, handler=progress_handler)
        elif mode == "UPGRADE":
            await installer.install_from_local(path, cmd="Upgrade", options=options, handler=progress_handler)
        else:
            raise RuntimeError(f"Invalid mode [{mode}]")

        info_plist_path = Path(path) / "Info.plist"

        with open(info_plist_path, 'rb') as f:
            plist_data = plistlib.load(f)

        bundle_identifier = plist_data["CFBundleIdentifier"]
        res = await installer.lookup(options={"BundleIDs": [bundle_identifier]})

        await ipc_client.write_reply({"id": id, "state": "completed", "result": res[bundle_identifier]["Path"]})
        print("Installed bundle: " + str(bundle_identifier))

async def get_bundle_identifier(id, path, ipc_client):
    info_plist_path = Path(path) / "Info.plist"
    with open(info_plist_path, 'rb') as f:
        plist_data = plistlib.load(f)

    bundle_identifier = plist_data["CFBundleIdentifier"]
    await ipc_client.write_reply({"id": id, "state": "completed", "result": bundle_identifier})


async def get_installed_path(id, lockdown_client, bundle_identifier, ipc_client):
    async with InstallationProxyService(lockdown=lockdown_client) as installer:
        res = await installer.lookup(options={"BundleIDs": [bundle_identifier]})

        if bundle_identifier not in res:
            reply = {"id": id, "state": "failed_not_installed"}
        else:
            reply = {"id": id, "state": "completed", "result": res[bundle_identifier]["Path"]}

        await ipc_client.write_reply(reply)

async def decode_plist(id, path, ipc_client):
    with open(path, 'rb') as f:
        plist_data = plistlib.load(f)
        await ipc_client.write_reply({"id": id, "state": "completed", "result": plist_data})

async def auto_mount_image(id, lockdown, ipc_client):
    try:
        await auto_mount(lockdown)
    except AlreadyMountedError:
        pass
    await ipc_client.write_reply({"id": id, "state": "completed"})


async def start_tunneld():
    if platform.system() == "Darwin":
        print("Starting tunneld as process")
        python_executable = sys.executable
        start_script = f'do shell script "sudo {python_executable} -m pymobiledevice3 remote tunneld --no-usb --no-wifi --no-mobdev2" with administrator privileges'
        print(f'Running "{start_script}"')
        process = await asyncio.create_subprocess_exec(
            'osascript', '-e', start_script,
            stdout=None, stderr=None
        )
        print(f"Started tunneld process with pid {process.pid}")
    else:
        print("Starting tunneld as task")

        asyncio.create_task(asyncio.to_thread(
            TunneldRunner.create, *TUNNELD_DEFAULT_ADDRESS, protocol=TunnelProtocol.DEFAULT
        ))

    for i in range(60):
        if await is_tunneld_running():
            print("tunneld successfully started")
            return
        await asyncio.sleep(1)
    raise RuntimeError("Failed launching tunneld service")


async def is_tunneld_running():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}/hello") as response:
                if response.status != 200:
                    return False
                data = await response.json()
                return data.get("message") == "Hello, I'm alive"
    except Exception:
        return False

async def shutdown_tunneld():
    if await is_tunneld_running():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}/shutdown") as response:
                    if response.status != 200:
                        raise RuntimeError("Status code was " + str(response.status))
                    print("tunneld shutdown request successful")
        except Exception as e:
            print("Failed tunneld teardown", e)

async def ensure_tunneld_running():
    if not await is_tunneld_running():
        await start_tunneld()
        print("tunneld is not running - starting")
    else:
        print("tunneld is already running")


async def _start_forwarder(forwarder, listen_event):
    """Start a forwarder task and wait for it to begin listening.

    Returns the task on success.  On timeout the task is cancelled and
    cleaned up before the exception propagates.
    """
    task = asyncio.create_task(forwarder.start())
    try:
        await asyncio.wait_for(listen_event.wait(), timeout=10)
    except asyncio.TimeoutError:
        if task.done():
            task.result()  # re-raises the actual exception
        # Forwarder never started listening — clean up.
        forwarder.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise
    return task


async def debugserver_connect(id, lockdown, port, ipc_client):
    try:
        discovery_service = await get_tunneld_device_by_udid(lockdown.udid)
        if not discovery_service:
            raise TunneldConnectionError()
    except TunneldConnectionError:
        await ipc_client.write_reply({"id": id, "state": "failed_no_tunneld"})
        return

    if Version(discovery_service.product_version) < Version('17.0'):
        service_name = 'com.apple.debugserver.DVTSecureSocketProxy'
    else:
        service_name = 'com.apple.internal.dt.remote.debugproxy'

    listen_event = asyncio.Event()
    forwarder = LockdownTcpForwarder(discovery_service, port, service_name, listening_event=listen_event)
    task = await _start_forwarder(forwarder, listen_event)
    resolved_port = forwarder.listening_port
    ipc_client.add_forwarder(resolved_port, TcpForwarderResource(forwarder, task))

    await ipc_client.write_reply({"id": id, "state": "completed", "result": {
        "host": "127.0.0.1",
        "port": resolved_port
    }})


async def debugserver_close(id, port, ipc_client):
    await ipc_client.close_forwarder(port)
    await ipc_client.write_reply({"id": id, "state": "completed"})


async def usbmux_forward_open(id, udid, remote_port, local_port, ipc_client):
    listen_event = asyncio.Event()
    forwarder = UsbmuxTcpForwarder(udid, remote_port, local_port, listening_event=listen_event)
    task = await _start_forwarder(forwarder, listen_event)
    resolved_port = forwarder.listening_port

    ipc_client.add_forwarder(resolved_port, TcpForwarderResource(forwarder, task))

    await ipc_client.write_reply({"id": id, "state": "completed", "result": {
        "host": "127.0.0.1",
        "local_port": resolved_port,
        "remote_port": remote_port
    }})


async def usbmux_forward_close(id, local_port, ipc_client):
    await ipc_client.close_forwarder(local_port)
    await ipc_client.write_reply({"id": id, "state": "completed"})


async def get_version(id, ipc_client):
    await ipc_client.write_reply({"id": id, "state": "completed", "result": IPC_VERSION})


async def handle_command(command, ipc_client):
    try:
        res = json.loads(command)
        id = res['id']
    except Exception as e:
        try:
            await ipc_client.write_reply({"request": command, "state": "failed", "error": repr(e), "backtrace": traceback.format_exc()})
        except Exception:
            print(f"{ipc_client.address}: Failed to send parse error reply (client disconnected?)")
        return

    try:
        command_type = res['command']
        if command_type == "exit":
            print(f"Exiting by request from {ipc_client.address}")
            await shutdown_tunneld()
            # Signal the server loop to shut down gracefully.
            if _shutdown_event is not None:
                _shutdown_event.set()
            return
        elif command_type == "list_devices":
            await list_devices(id, ipc_client)
        elif command_type == "list_devices_udid":
            await list_devices_udid(id, ipc_client)
        elif command_type == "get_device":
            device_id = res.get('device_id')
            await get_device(id, device_id, ipc_client)
        elif command_type == "decode_plist":
            await decode_plist(id, res['plist_path'], ipc_client)
        elif command_type == "debugserver_close":
            await debugserver_close(id, res['port'], ipc_client)
        elif command_type == "usbmux_forwarder_open":
            await usbmux_forward_open(id, res['device_id'], res['remote_port'], res['local_port'], ipc_client)
        elif command_type == "usbmux_forwarder_close":
            await usbmux_forward_close(id, res['local_port'], ipc_client)
        elif command_type == "ensure_tunneld_running":
            await ensure_tunneld_running()
            await ipc_client.write_reply({"id": id, "state": "completed"})
        elif command_type == "is_tunneld_running":
            result = await is_tunneld_running()
            await ipc_client.write_reply({"id": id, "state": "completed", "result": result})
        elif command_type == "get_version":
            await get_version(id, ipc_client)
        elif command_type == "get_bundle_identifier":
            await get_bundle_identifier(id, res["app_path"], ipc_client)
        else:
            # Device-targeted commands
            device_id = res['device_id']
            async with await create_using_usbmux(device_id) as lockdown:
                if command_type == "install_app":
                    await install_app(id, lockdown, res['app_path'], res['install_mode'], ipc_client)
                elif command_type == "auto_mount_image":
                    await auto_mount_image(id, lockdown, ipc_client)
                elif command_type == "debugserver_connect":
                    await debugserver_connect(id, lockdown, res['port'], ipc_client)
                elif command_type == "get_installed_path":
                    await get_installed_path(id, lockdown, res["bundle_identifier"], ipc_client)
    except Exception as e:
        try:
            await ipc_client.write_reply({"id": id, "request": command, "state": "failed", "error": repr(e), "backtrace": traceback.format_exc()})
        except Exception:
            print(f"{ipc_client.address}: Failed to send error reply for command {id} (client disconnected?)")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    address = writer.get_extra_info('peername')
    try:
        # Version handshake: read 1 byte (client version), send ours
        version_data = await asyncio.wait_for(reader.read(1), timeout=2)
        if not version_data:
            print(f"{address}: Failed to receive version")
            writer.close()
            await writer.wait_closed()
            return

        java_protocol_version = version_data[0]
        writer.write(bytes([IPC_VERSION]))
        await writer.drain()

        ipc_client = IPCClient(reader, writer, address, java_protocol_version)
        print(f"Connected {address} with version {java_protocol_version}")
    except Exception:
        print(f"{address}: Failed to send version")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            command = line.decode().strip()
            if not command:
                break
            print(f"{address}: Received command: {command}")
            # Each command runs as its own task so one slow command doesn't block the next from the same client.
            # Tasks are tracked so they can be cancelled on disconnect.
            task = asyncio.create_task(handle_command(command, ipc_client))
            ipc_client.track_task(task)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"{address}: Read error: {e}")
    finally:
        await ipc_client.close()
        print(f"Disconnected {address}")


async def async_main():
    global _shutdown_event

    if len(sys.argv) < 2:
        print("Usage: python handler.py <port_file>")
        sys.exit(1)

    _shutdown_event = asyncio.Event()

    server = await asyncio.start_server(handle_client, '127.0.0.1', 0)
    if len(server.sockets) != 1:
        print("Somehow opened two ports??")
        sys.exit(1)

    port = server.sockets[0].getsockname()[1]
    path = sys.argv[1]

    with open(path, "w") as f:
        f.write(str(port))

    print(f"Written port {port} to file {path}")
    print(f"Start listening on port {port}")

    try:
        # Run until the shutdown event is set (by the "exit" command)
        # or the server is cancelled externally.
        async with server:
            await _shutdown_event.wait()
            print("Shutdown requested, closing server...")
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        await server.wait_closed()
        try:
            os.remove(path)
        except OSError:
            pass

    print("Closed server")


def main():
    asyncio.run(async_main())


main()