import math
import time
from typing import Callable

from pymodbus import FramerType
from pymodbus.client import ModbusSerialClient
from pymodbus.pdu import ModbusPDU

# ---------------------------
# Константы
# ---------------------------
FUNC_CODE_FIRMWARE = 0x2A
FUNC_CODE_START = 0x2B

MAX_PACKET_SIZE = 91
HEADER_SIZE = 5
CRC_SIZE = 2
MAX_PAYLOAD_SIZE = MAX_PACKET_SIZE - HEADER_SIZE - CRC_SIZE

MAX_RETRIES = 3  # число попыток отправки пакета
RETRY_DELAY = 1  # задержка между повторами


class StartUpdateRequest(ModbusPDU):
    """PDU для запуска обновления."""

    function_code = FUNC_CODE_START

    def __init__(self, *, unit: int = 1, **kwargs):
        super().__init__(dev_id=unit, **kwargs)

    def encode(self) -> bytes:
        return b""


class StartUpdateResponse(ModbusPDU):
    function_code = FUNC_CODE_START

    @classmethod
    def calculateRtuFrameSize(cls, data: bytes) -> int:  # type: ignore[override]
        return len(data)

    def decode(self, data: bytes) -> None:  # type: ignore[override]
        pass


class FirmwareRequest(ModbusPDU):
    """PDU отправки блока прошивки."""

    function_code = FUNC_CODE_FIRMWARE

    def __init__(self, packet_index: int, total_packets: int, payload: bytes, *, unit: int = 1, **kwargs):
        super().__init__(dev_id=unit, **kwargs)
        self.packet_index = packet_index
        self.total_packets = total_packets
        self.payload = payload

    def encode(self) -> bytes:  # type: ignore[override]
        msg = bytearray()
        msg += self.packet_index.to_bytes(2, "big")
        msg += self.total_packets.to_bytes(2, "big")
        msg += self.payload
        return bytes(msg)


class FirmwareResponse(ModbusPDU):
    function_code = FUNC_CODE_FIRMWARE

    @classmethod
    def calculateRtuFrameSize(cls, data: bytes) -> int:  # type: ignore[override]
        return len(data)

    def decode(self, data: bytes) -> None:  # type: ignore[override]
        pass


# ---------------------------------------------------------------------------
# Функция отправки прошивки
# ---------------------------------------------------------------------------

def send_firmware(
    filename: str,
    port: str,
    slave_id: int,
    progress_cb: Callable[[int, str], None] | None = None,
    *,
    skip_start: bool = False,
) -> bool:
    """\
    Читает файл и по рскому связи отправляет его на устройство.

    :param filename: путь к файлу .hex
    :param port: серийный порт
    :param slave_id: ID в Modbus
    :param progress_cb: колбек для обновления прогресса
    :param skip_start: пропустить отправку команды старта
    :return: True при успехе
    """
    try:
        with open(filename, "rb") as f:
            firmware_data = f.read()
    except Exception:
        return False

    total_packets = math.ceil(len(firmware_data) / MAX_PAYLOAD_SIZE)

    client = ModbusSerialClient(
        port=port,
        framer=FramerType.RTU,
        baudrate=115200,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=1,
    )
    if not client.connect():
        return False

    client.register(StartUpdateRequest)
    client.register(StartUpdateResponse)
    client.register(FirmwareRequest)
    client.register(FirmwareResponse)

    if not skip_start:
        if progress_cb:
            progress_cb(0, "подготовка к обновлению")
        start_req = StartUpdateRequest(unit=slave_id)
        start_res = client.execute(False, start_req)
        if not start_res:
            client.close()
            return False
        time.sleep(7)
    else:
        if progress_cb:
            progress_cb(0, "")

    first_ack = False
    for i in range(total_packets):
        idx = i + 1
        chunk = firmware_data[i * MAX_PAYLOAD_SIZE : (i + 1) * MAX_PAYLOAD_SIZE]
        for attempt in range(1, MAX_RETRIES + 1):
            req = FirmwareRequest(idx, total_packets, chunk, unit=slave_id)
            res = client.execute(False, req)
            if res:
                first_ack = True
                percent = int(idx * 100 / total_packets)
                if progress_cb:
                    message = "" if first_ack else "подготовка к обновлению"
                    progress_cb(percent, message)
                break
            time.sleep(RETRY_DELAY)
        else:
            client.close()
            return False

    client.close()
    if progress_cb:
        progress_cb(100, "")
    return True

from PySide6.QtCore import QObject, Signal

class FirmwareUpdateWorker(QObject):
    """\
    Рабочий объект для обновления ПО.
    Использует функцию send_firmware и передаёт сигналы в UI.
    """

    progress = Signal(int, str)
    finished = Signal(bool)

    def __init__(self, port: str, slave: int, filename: str):
        super().__init__()
        self.port = port
        self.slave = slave
        self.filename = filename

    def run(self) -> None:
        result = send_firmware(
            self.filename,
            port=self.port,
            slave_id=self.slave,
            progress_cb=self.progress.emit,
        )
        self.finished.emit(result)
