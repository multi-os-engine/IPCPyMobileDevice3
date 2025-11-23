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


import atexit
import json
import os
import platform
import queue
import select
import subprocess
import sys
import threading
import traceback
from pathlib import Path
import asyncio

from time import sleep
from typing import Union

import requests
from packaging.version import Version

import pymobiledevice3.usbmux as usbmux
from pymobiledevice3.exceptions import *
from pymobiledevice3.remote.common import TunnelProtocol
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.mobile_image_mounter import auto_mount
from pymobiledevice3.tcp_forwarder import LockdownTcpForwarder, UsbmuxTcpForwarder, TcpForwarderBase
from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid, TUNNELD_DEFAULT_ADDRESS
from pymobiledevice3.tunneld.server import TunneldRunner
from pymobiledevice3.usbmux import *
from pymobiledevice3.lockdown import create_using_usbmux
import plistlib

IPC_VERSION = 3

class ConnectionResource:
    def close(self):
        raise NotImplementedError()

class TcpForwarderResource(ConnectionResource):

    def __init__(self, forwarder: TcpForwarderBase, thread):
        self.forwarder = forwarder
        self.thread = thread

    def close(self):
        self.forwarder.stop()
        self.thread.join(5)

        if self.thread.is_alive():
            print(f"Joining forwarder thread {self.forwarder.src_port} timed out")


class IPCClient:
    def __init__(self, sock, address, version):
        self.sock = sock
        self.read_file = sock.makefile('r')
        self.write_file = sock.makefile('w')
        self.address = address
        self.version = version
        self.active_forwarder: dict[int, TcpForwarderResource] = {}

    def close(self):
        for resource in self.active_forwarder.values():
            resource.close()
        self.active_forwarder.clear()

        self.sock.close()
        self.read_file.close()
        self.write_file.close()

    def add_forwarder(self, local_port: int, forwarder: TcpForwarderResource):
        if local_port in self.active_forwarder:
            raise ValueError(f"{local_port} already has an open connection: {self.active_forwarder[local_port]}")
        self.active_forwarder[local_port] = forwarder

    def close_forwarder(self, local_port: int):
        if local_port not in self.active_forwarder:
            raise ValueError(f"Port {local_port} is not open")
        forwarder = self.active_forwarder.pop(local_port)
        forwarder.close()

class WriteDispatcher:
    def __init__(self):
        self.write_queue = queue.Queue()
        self.shutdown_event = threading.Event()
        self.write_thread = threading.Thread(target=self._write_worker, daemon=True, name="Python-WriteDispatcher")
        self.write_thread.start()

    def _write_worker(self):
        try:
            while True:
                try:
                    writer, message = self.write_queue.get()
                    if message is None:
                        break

                    writer.write(str(message))
                    writer.write("\n")
                    writer.flush()

                except Exception as e:
                    print(f"Write thread error: {e}")
                    continue
        finally:
            self.shutdown_event.set()

    def write_reply(self, ipc_client, reply: dict):
        if self.shutdown_event.is_set():
            return
        print(f"{ipc_client.address}: Sending packet: {str(reply)}")
        self.write_queue.put((ipc_client.write_file, reply))

    def shutdown(self):
        self.write_queue.put(None)
        self.write_thread.join()

class ReadDispatcher:
    def __init__(self):
        self.socket_list = []
        self.clients = {}
        self.shutdown_event = threading.Event()
        self.read_thread = threading.Thread(target=self._read_worker, daemon=True, name="Python-ReadDispatcher")
        self.read_thread.start()

    def _read_worker(self):
        try:
            while True:
                try:
                    ready_sockets, _, exception_sockets = select.select(self.socket_list, [], self.socket_list, 1.0)

                    if self.shutdown_event.is_set():
                        for sock in self.socket_list:
                            self.remove_client(self.clients[sock])
                        return

                    for exception_socket in exception_sockets:
                        self.remove_client(self.clients[exception_socket])

                    for ready_socket in ready_sockets:
                        ipc_client = self.clients[ready_socket]
                        command = ipc_client.read_file.readline().strip()
                        if not command:
                            self.remove_client(ipc_client)
                            continue
                        print(f"{ipc_client.address}: Received command: {command}")

                        handle_command(command, ipc_client)

                except Exception as e:
                    print(f"Read thread error: {e}")
                    continue
        finally:
            self.shutdown_event.set()

    def remove_client(self, ipc_client):
        self.socket_list.remove(ipc_client.sock)
        self.clients.pop(ipc_client.sock)
        ipc_client.close()
        print(f"Disconnected {ipc_client.address}")

    def add_client(self, ipc_client):
        self.clients[ipc_client.sock] = ipc_client
        self.socket_list.append(ipc_client.sock)
        print(f"Connected {ipc_client.address} with version {ipc_client.version}")


    def shutdown(self):
        self.shutdown_event.set()

write_dispatcher = WriteDispatcher()
read_dispatcher = ReadDispatcher()


def list_devices(id, ipc_client):
    devices = []
    for device in usbmux.list_devices():
        udid = device.serial

        with create_using_usbmux(udid, autopair=False, connection_type=device.connection_type) as lockdown:
            devices.append(lockdown.short_info)

    reply = {"id": id, "state": "completed", "result": devices}

    write_dispatcher.write_reply(ipc_client, reply)

def list_devices_udid(id, ipc_client):
    devices = []
    for device in usbmux.list_devices():
        devices.append(device.serial)

    reply = {"id": id, "state": "completed", "result": devices}

    write_dispatcher.write_reply(ipc_client, reply)

def get_device(id, device_id, ipc_client):
    try:
        with create_using_usbmux(device_id, autopair=False) as lockdown:
            reply = {"id": id, "state": "completed", "result": lockdown.short_info}
            write_dispatcher.write_reply(ipc_client, reply)
    except Union[NoDeviceConnectedError, DeviceNotFoundError]:
        reply = {"id": id, "state": "no_device"}
        write_dispatcher.write_reply(ipc_client, reply)

def install_app(id, lockdown_client, path, mode, ipc_client):
    with InstallationProxyService(lockdown=lockdown_client) as installer:
        options = {"PackageType": "Developer"}

        def progress_handler(progress, *args):
            reply = {"id": id, "state": "progress", "progress": progress}
            write_dispatcher.write_reply(ipc_client, reply)
            return

        if mode == "INSTALL":
            installer.install(path, options=options, handler=progress_handler)
        elif mode == "UPGRADE":
            installer.upgrade(path, options=options, handler=progress_handler)
        else:
            raise RuntimeError(f"Invalid mode [{mode}]")

        info_plist_path = Path(path) / "Info.plist"

        with open(info_plist_path, 'rb') as f:
            plist_data = plistlib.load(f)

        bundle_identifier = plist_data["CFBundleIdentifier"]

        res = installer.lookup(options={"BundleIDs" : [bundle_identifier]})

        reply = {"id": id, "state": "completed", "result": res[bundle_identifier]["Path"]}
        write_dispatcher.write_reply(ipc_client, reply)

        print("Installed bundle: " + str(bundle_identifier))

def get_bundle_identifier(id, path, ipc_client):
    info_plist_path = Path(path) / "Info.plist"
    with open(info_plist_path, 'rb') as f:
        plist_data = plistlib.load(f)

    bundle_identifier = plist_data["CFBundleIdentifier"]

    reply = {"id": id, "state": "completed", "result": bundle_identifier}
    write_dispatcher.write_reply(ipc_client, reply)

def get_installed_path(id, lockdown_client, bundle_identifier, ipc_client):
    with InstallationProxyService(lockdown=lockdown_client) as installer:
        res = installer.lookup(options={"BundleIDs" : [bundle_identifier]})

        if bundle_identifier not in res:
            reply = {"id": id, "state": "not_installed"}
        else:
            reply = {"id": id, "state": "completed", "result": res[bundle_identifier]["Path"]}

        write_dispatcher.write_reply(ipc_client, reply)

def decode_plist(id, path, ipc_client):
    with open(path, 'rb') as f:
        plist_data = plistlib.load(f)
        reply = {"id": id, "state": "completed", "result": plist_data}
        write_dispatcher.write_reply(ipc_client, reply)
        return

def auto_mount_image(id, lockdown, ipc_client):
    try:
        asyncio.run(auto_mount(lockdown))
    except AlreadyMountedError:
        pass
    reply = {"id": id, "state": "completed"}
    write_dispatcher.write_reply(ipc_client, reply)


def start_tunneld():
    if platform.system() == "Darwin":
        print("Starting tunneld as process")
        python_executable = sys.executable
        start_script = f'do shell script "sudo {python_executable} -m pymobiledevice3 remote tunneld" with administrator privileges'
        print("Running \"" + start_script + "\"")
        process = subprocess.Popen(['osascript', '-e', start_script], stdout=None, stderr=None)
        print(f"Started tunneld process with pid {process.pid}")
    else:
        print("Starting tunneld as thread")
        def _worker():
            TunneldRunner.create(*TUNNELD_DEFAULT_ADDRESS, protocol=TunnelProtocol.DEFAULT)

        webserver_thread = threading.Thread(target=_worker, daemon=True, name="Python-Tunneld")
        webserver_thread.start()

    for i in range(60):
        if is_tunneld_running():
            print("tunneld successfully started")
            return
        sleep(1)
    raise RuntimeError("Failed launching tunneld service")

def is_tunneld_running():
    try:
        response = requests.get(f"http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}/hello")
        if response.status_code != 200:
            raise RuntimeError()

        data = response.json()
        if data.get("message") != "Hello, I'm alive":
            raise RuntimeError()
        # Tunneld seems running happily
        return True
    except Exception:
        return False

def shutdown_tunneld():
    if is_tunneld_running():
        try:
            response = requests.get(f"http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}/shutdown")
            if response.status_code != 200:
                raise RuntimeError("Status code was " + str(response.status_code))
            print("tunneld shutdown request successful")
        except Exception as e:
            print("Failed tunneld teardown", e)

def ensure_tunneld_running():
    if not is_tunneld_running():
        start_tunneld()
        print("tunneld is not running - starting")
    else:
        print("tunneld is already running")

def debugserver_connect(id, lockdown, port, ipc_client):
    try:
        discovery_service = get_tunneld_device_by_udid(lockdown.udid)
        if not discovery_service:
            raise TunneldConnectionError()
    except TunneldConnectionError:
        reply = {"id":id, "state": "failed_tunneld"}
        write_dispatcher.write_reply(ipc_client, reply)
        return

    if Version(discovery_service.product_version) < Version('17.0'):
        service_name = 'com.apple.debugserver.DVTSecureSocketProxy'
    else:
        service_name = 'com.apple.internal.dt.remote.debugproxy'
    listen_event = threading.Event()
    forwarder = LockdownTcpForwarder(discovery_service, port, service_name, listening_event=listen_event)

    def forwarder_thread():
        forwarder.start()

    thread = threading.Thread(target=forwarder_thread, daemon=True)
    thread.start()

    listen_event.wait()
    selected_port = forwarder.server_socket.getsockname()[1]

    resource = TcpForwarderResource(forwarder, thread)
    ipc_client.add_forwarder(selected_port, resource)

    reply = {"id": id, "state": "completed", "result": {
        "host": "127.0.0.1",
        "port": selected_port
    }}
    write_dispatcher.write_reply(ipc_client, reply)


def debugserver_close(id, port, ipc_client):
    ipc_client.close_forwarder(port)

    reply = {"id": id, "state": "completed"}
    write_dispatcher.write_reply(ipc_client, reply)


def usbmux_forward_open(id, udid, remote_port, local_port, ipc_client):
    listen_event = threading.Event()

    forwarder = UsbmuxTcpForwarder(udid, remote_port, local_port, listening_event=listen_event)

    def forwarder_thread():
        forwarder.start()

    thread = threading.Thread(target=forwarder_thread, daemon=True)
    thread.start()

    listen_event.wait()
    selected_port = forwarder.server_socket.getsockname()[1]

    resource = TcpForwarderResource(forwarder, thread)
    ipc_client.add_forwarder(selected_port, resource)

    reply = {"id": id, "state": "completed", "result": {
        "host": "127.0.0.1",
        "local_port": selected_port,
        "remote_port": remote_port
    }}
    write_dispatcher.write_reply(ipc_client, reply)


def usbmux_forward_close(id, local_port, ipc_client):
    ipc_client.close_forwarder(local_port)

    reply = {"id": id, "state": "completed"}
    write_dispatcher.write_reply(ipc_client, reply)

def get_version(id, ipc_client):
    reply = {"id": id, "state": "completed", "result": IPC_VERSION}
    write_dispatcher.write_reply(ipc_client, reply)

def handle_command(command, ipc_client):
    try:
        res = json.loads(command)
        id = res['id']
    except Exception as e:
        reply = {"request": command, "state": "failed", "error": repr(e), "backtrace": traceback.format_exc()}
        write_dispatcher.write_reply(ipc_client, reply)
        return

    try:
        command_type = res['command']
        if command_type == "exit":
            print(f"Exiting by request from {ipc_client.address}")
            atexit._run_exitfuncs()
            os._exit(0)
        elif command_type == "list_devices":
            list_devices(id, ipc_client)
            return
        elif command_type == "list_devices_udid":
            list_devices_udid(id, ipc_client)
            return
        elif command_type == "get_device":
            device_id = res['device_id'] if 'device_id' in res else None
            get_device(id, device_id, ipc_client)
            return
        elif command_type == "decode_plist":
            decode_plist(id, res['plist_path'], ipc_client)
            return
        elif command_type == "debugserver_close":
            debugserver_close(id, res['port'], ipc_client)
            return
        elif command_type == "usbmux_forwarder_open":
            usbmux_forward_open(id, res['device_id'], res['remote_port'], res['local_port'], ipc_client)
            return
        elif command_type == "usbmux_forwarder_close":
            usbmux_forward_close(id, res['local_port'], ipc_client)
            return
        elif command_type == "ensure_tunneld_running":
            ensure_tunneld_running()
            reply = {"id": id, "state": "completed"}
            write_dispatcher.write_reply(ipc_client, reply)
            return
        elif command_type == "is_tunneld_running":
            res = is_tunneld_running()
            reply = {"id": id, "state": "completed", "result": res}
            write_dispatcher.write_reply(ipc_client, reply)
            return
        elif command_type == "get_version":
            get_version(id, ipc_client)
            return
        elif command_type == "get_bundle_identifier":
            get_bundle_identifier(id, res["app_path"], ipc_client)
            return

        # Now come the device targetted functions
        device_id = res['device_id']
        with create_using_usbmux(device_id) as lockdown:
            if command_type == "install_app":
                install_app(id, lockdown, res['app_path'], res['install_mode'], ipc_client)
                return
            elif command_type == "auto_mount_image":
                auto_mount_image(id, lockdown, ipc_client)
                return
            elif command_type == "debugserver_connect":
                debugserver_connect(id, lockdown, res['port'], ipc_client)
                return
            elif command_type == "get_installed_path":
                get_installed_path(id, lockdown, res["bundle_identifier"], ipc_client)
                return
    except Exception as e:
        reply = {"id": id, "request": command, "state": "failed", "error": repr(e), "backtrace": traceback.format_exc()}
        write_dispatcher.write_reply(ipc_client, reply)
        return

def main():
    if len(sys.argv) < 3:
        print("Usage: python handler.py <port_file>")
        sys.exit(1)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('localhost', 0))
    server.listen(5)

    port = server.getsockname()[1]
    path = sys.argv[2]

    with open(path, "w") as f:
        f.write(str(port))

    print(f"Written port {port} to file {path}")

    atexit.register(os.remove, path)
    atexit.register(shutdown_tunneld)
    print(f"Start listening on port {port}")
    while True:
        client_socket, client_address = server.accept()
        try:
            reads, _, _ = select.select([client_socket], [], [], 2)
            if client_socket not in reads:
                raise RuntimeError()
            java_protocol_version = client_socket.recv(1)[0]
            client_socket.send(bytes([IPC_VERSION]))

            read_dispatcher.add_client(IPCClient(client_socket, client_address, java_protocol_version))
        except Exception:
            print(f"{client_address}: Failed to send version")


main()