## Overview

This project exposes [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) via an JSon IPC protocol via TCP.  


## How to launch
It can be launched via `python3 handler.py <port_file>`.  
`port_file` specifies a file, the IPC handler will write the port it listenes to.

## JSon API
The API is relativly simpel. Every command send needs and `id` field and a `command` field.  
The `id` needs to be unique, because it is used for replying. It only needs to be unique per-connection.  
For available `command` check at the command list below.  
If a command fails unexpectedly, the response will have `state` set to `failed`, along with `error` containing the exception message and `backtrace` containing the full stack trace.    
The response will also include `request` with the original command string.  
If the JSON parsing failed (and no id could be extracted), the response will not include an `id` field.

## Commands

| Command                  | Arguments                                                        | Responses                                                                          | Description                                                                       |
|--------------------------|------------------------------------------------------------------|------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| `exit`                   | None                                                             | None (process exits)                                                               | Exits the IPC handler process                                                     |
| `list_devices`           | None                                                             | `completed`: `result` = array of device info objects                               | Lists all connected devices with their full info                                  |
| `list_devices_udid`      | None                                                             | `completed`: `result` = array of UDID strings                                      | Lists UDIDs of all connected devices                                              |
| `get_device`             | `device_id` (optional)                                           | `completed`: `result` = device info object, `failed_no_device`: device not found   | Gets info for a specific device                                                   |
| `decode_plist`           | `plist_path`                                                     | `completed`: `result` = decoded plist dictionary to json                           | Decodes a plist file at the given path to json                                    |
| `get_bundle_identifier`  | `app_path`                                                       | `completed`: `result` = bundle identifier string                                   | Extracts CFBundleIdentifier from app's Info.plist                                 |
| `get_version`            | None                                                             | `completed`: `result` = IPC version number                                         | Returns the IPC protocol version                                                  |
| `is_tunneld_running`     | None                                                             | `completed`: `result` = boolean                                                    | Checks if tunneld service is running                                              |
| `ensure_tunneld_running` | None                                                             | `completed`                                                                        | Starts tunneld if not already running. Will ask for elevated privileges on macOS. |
| `install_app`            | `device_id`, `app_path`, `install_mode` ("INSTALL" or "UPGRADE") | `progress`: `progress` = percentage, `completed`: `result` = installed path        | Installs or upgrades an app on the device                                         |
| `auto_mount_image`       | `device_id`                                                      | `completed`                                                                        | Auto-mounts the developer disk image                                              |
| `get_installed_path`     | `device_id`, `bundle_identifier`                                 | `completed`: `result` = path string, `failed_not_installed`: app not found         | Gets the installed path for a bundle identifier                                   |
| `debugserver_connect`    | `device_id`, `port`                                              | `completed`: `result` = `{host, port}`, `failed_no_tunneld`: tunneld not connected | Opens a TCP forwarder to the device's debugserver                                 |
| `debugserver_close`      | `port`                                                           | `completed`                                                                        | Closes a debugserver TCP forwarder                                                |
| `usbmux_forwarder_open`  | `device_id`, `remote_port`, `local_port`                         | `completed`: `result` = `{host, local_port, remote_port}`                          | Opens a usbmux TCP port forwarder                                                 |
| `usbmux_forwarder_close` | `local_port`                                                     | `completed`                                                                        | Closes a usbmux TCP port forwarder                                                |
