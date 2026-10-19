"""Explicit TCP/USB operations for the Juliet cutter.

No discovery probes, preamble, acknowledgement or automatic retries. Returning
success means bytes were delivered to the transport, not that a cut completed.
"""
import ipaddress
import socket
import time

from .i18n import tr

USB_VENDOR = 0x0483
USB_PRODUCT = 0x5750
TCP_PORT = 8080
TCP_CHUNK_DELAY = 0.3  # Pace consecutive writes for the cutter's receive buffer.


def _ipv4_endpoint(host, port):
    address = ipaddress.IPv4Address(host.strip())
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ValueError(tr("請輸入 Juliet 螢幕顯示的單一 IPv4 位址"))
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(tr("連接埠必須介於 1–65535"))
    return str(address)


def send_tcp(payload, host, port=TCP_PORT, timeout=2.0, *, write_timeout=60.0, on_progress=None):
    """Stream raw PLT in paced chunks, without reconnecting or replaying.

    Connection setup has its own short timeout. A slow receiver may pause each
    write for up to write_timeout. The total budget is at least five minutes,
    or the expected pacing time plus one minute for larger jobs. Short
    writes add their extra pacing time without reconnecting or replaying.
    The returned count is bytes accepted by the local socket, not device ACKs.
    """
    address = _ipv4_endpoint(host, port)
    if not payload:
        raise ValueError(tr("沒有可傳送的切割資料"))
    with socket.create_connection((address, port), timeout=timeout) as connection:
        sent = 0
        chunks = (len(payload) + 1023) // 1024
        deadline = time.monotonic() + max(300.0, chunks * TCP_CHUNK_DELAY + 60.0)
        data = memoryview(payload)
        try:
            while sent < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("transfer deadline exceeded")
                connection.settimeout(min(write_timeout, remaining))
                chunk = data[sent:sent + 1024]
                count = connection.send(chunk)
                if count == 0:
                    raise ConnectionError("connection closed")
                if count < len(chunk):
                    deadline += TCP_CHUNK_DELAY  # Budget the extra paced write.
                sent += count
                if on_progress is not None:
                    on_progress(sent, len(data))
                if TCP_CHUNK_DELAY:
                    time.sleep(TCP_CHUNK_DELAY)
        except OSError as cause:
            message = tr("TCP 傳輸中斷：本機已接受 {sent}/{total} bytes；不會自動重送。原因：{reason}",
                         sent=sent, total=len(data), reason=str(cause))
            error = TimeoutError(message) if isinstance(cause, TimeoutError) else OSError(message)
            error.bytes_sent, error.bytes_total = sent, len(data)
            raise error from cause
    return sent


def query_tcp_version(host, port=TCP_PORT, timeout=2.0):
    """Read the firmware version without sending motion commands."""
    address = _ipv4_endpoint(host, port)
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((address, port), timeout=timeout) as connection:
            connection.sendall(b";RSVER;")
            response = bytearray()
            while len(response) < 256:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                chunk = connection.recv(256 - len(response))
                if not chunk:
                    break
                response.extend(chunk)
                if b";" in response:
                    break
    except (TimeoutError, socket.timeout) as error:
        raise TimeoutError(tr("Juliet 版本查詢逾時。")) from error
    try:
        text = response.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(tr("Juliet 回覆格式無效，無法讀取韌體版本。")) from error
    if not text.endswith(";") or text.count(";") != 1 or not text.startswith("SVER=") or len(text) <= 6:
        raise ValueError(tr("Juliet 回覆格式無效，無法讀取韌體版本。"))
    return text[5:-1]


def usb_devices():
    """Enumerate devices matching the supported cutter VID/PID."""
    import usb.core
    return list(usb.core.find(find_all=True, idVendor=USB_VENDOR, idProduct=USB_PRODUCT))


def send_usb(payload, device, timeout_ms=2000):
    """Use the printer-class interface and last matching bulk IN/OUT pair.

    Endpoints are descriptors, not guessed addresses. Writes handle short
    transfers. No kernel driver is detached automatically.
    """
    import usb.util
    if not payload:
        raise ValueError(tr("沒有可傳送的切割資料"))
    if (device.idVendor, device.idProduct) != (USB_VENDOR, USB_PRODUCT):
        raise ValueError(tr("所選 USB 裝置不符合支援的切割機識別碼"))
    try:
        configuration = device.get_active_configuration()
    except Exception:
        device.set_configuration()
        configuration = device.get_active_configuration()
    interfaces = []
    for interface in configuration:
        incoming = outgoing = None
        if interface.bInterfaceClass != 7:
            continue
        for endpoint in interface:
            if usb.util.endpoint_type(endpoint.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                if usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_IN:
                    incoming = endpoint
                else:
                    outgoing = endpoint
        if incoming is not None and outgoing is not None:
            interfaces.append((interface, outgoing))
    if len(interfaces) != 1:
        raise ValueError(tr("未找到唯一可用的 USB bulk 介面"))
    interface, endpoint = interfaces[0]
    claimed = False
    try:
        usb.util.claim_interface(device, interface.bInterfaceNumber)
        claimed = True
        if interface.bAlternateSetting:
            device.set_interface_altsetting(interface=interface.bInterfaceNumber,
                                            alternate_setting=interface.bAlternateSetting)
        sent = 0
        while sent < len(payload):
            chunk = payload[sent:sent + 1024]
            count = endpoint.write(chunk, timeout=timeout_ms)
            if not 0 < count <= len(chunk):
                raise OSError(tr("USB 傳輸未完成；不會自動重送已送出的資料"))
            sent += count
        return sent
    finally:
        if claimed:
            usb.util.release_interface(device, interface.bInterfaceNumber)
        usb.util.dispose_resources(device)
