import sys
from contextlib import contextmanager

from PySide6.QtCore    import Qt, QObject, Signal, QThread
from PySide6.QtWidgets import QApplication, QMainWindow, QComboBox, QFileDialog, QMessageBox
from PySide6.QtCore import QTimer
import math
import serial
import serial.tools.list_ports
import struct
import time
import resources_rc
from PySide6.QtGui import QIcon

from ui_main import Ui_MainWindow

# --- константы для обновления ---
FUNC_CODE_FIRMWARE = 0x2A
FUNC_CODE_START    = 0x2B
BOOTLOADER_ID      = 1

MAX_PACKET_SIZE    = 91
HEADER_SIZE        = 5
CRC_SIZE           = 2
MAX_PAYLOAD_SIZE   = MAX_PACKET_SIZE - HEADER_SIZE - CRC_SIZE

MAX_RETRIES        = 3
RETRY_DELAY        = 1

# --- сдвиг карты регистров --------------------------------------------
REGISTER_OFFSET = 9  # учитываем добавление 9 неиспользуемых регистров
REG_SENSOR_READ_START = 22 + REGISTER_OFFSET  # начало блока датчиков с учётом смещения
REG_SENSOR_READ_COUNT = 8  # размер блока датчиков (не изменился, только адрес)
REG_PORT_SENSOR_BIND = 17 + REGISTER_OFFSET  # регистр привязки порта, датчика и режима калибровки
REG_CAL_POINT_X1 = 18 + REGISTER_OFFSET  # точка калибровки X1
REG_CAL_POINT_Y1 = 19 + REGISTER_OFFSET  # точка калибровки Y1
REG_CAL_POINT_X2 = 20 + REGISTER_OFFSET  # точка калибровки X2
REG_CAL_POINT_Y2 = 21 + REGISTER_OFFSET  # точка калибровки Y2
REG_BAUD = 30 + REGISTER_OFFSET  # регистр скорости UART
REG_BITS = 31 + REGISTER_OFFSET  # регистр количества бит данных
REG_PARITY = 32 + REGISTER_OFFSET  # регистр чётности
REG_STOP = 33 + REGISTER_OFFSET  # регистр стоп-битов
REG_PASSWORD = 34 + REGISTER_OFFSET  # регистр пароля
REG_USART_ID = 35 + REGISTER_OFFSET  # регистр номера USART

# --- настройки USART для автоподключения и отправки 0x2A ---------
# используются при приёме пакета автоподключения и во время
# обновления прошивки
DEFAULT_BAUDRATE = 56000  # скорость по умолчанию для автоподключения (115 200 бод)
DEFAULT_BYTESIZE = serial.EIGHTBITS
DEFAULT_PARITY   = serial.PARITY_NONE
DEFAULT_STOPBITS = serial.STOPBITS_ONE

# --- параметры автозапроса настроек -----------------------------------
AUTO_CONNECT_CMD = b"\x41"      # команда, которую нужно слать устройству в режиме автоподключения
AUTO_CONNECT_INTERVAL = 0.2     # период отправки команды 0x41 в секундах (200 мс)
AUTO_CONNECT_RESPONSE_LEN = 17  # ожидаемый размер ответа с настройками (байт)






class AutoConnectWorker(QObject):
    """Поток для приёма Modbus пакета с настройками."""

    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, port: str):
        super().__init__()
        self.port = port
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            ser = serial.Serial(
                self.port,
                baudrate=DEFAULT_BAUDRATE,
                bytesize=DEFAULT_BYTESIZE,
                parity=DEFAULT_PARITY,
                stopbits=DEFAULT_STOPBITS,
                timeout=AUTO_CONNECT_INTERVAL,  # выставляем таймаут, равный требуемой паузе между командами
            )
        except serial.SerialException as exc:
            self.error.emit(str(exc))
            return

        try:
            ser.reset_input_buffer()  # очищаем входной буфер перед началом обмена
        except serial.SerialException:
            pass

        while self._running:
            loop_started = time.monotonic()  # фиксируем время, чтобы выдержать паузу 200 мс
            try:
                ser.write(AUTO_CONNECT_CMD)  # отправляем команду 0x41 на устройство
            except serial.SerialException as exc:
                self.error.emit(str(exc))
                break

            data = ser.read(AUTO_CONNECT_RESPONSE_LEN)  # ждём ответ с настройками
            if len(data) >= AUTO_CONNECT_RESPONSE_LEN and self._check_crc(data):
                settings = self._parse_regs(data)
                ser.close()  # сразу закрываем порт, чтобы освободить ресурс перед выходом из потока
                self.finished.emit(settings)
                return

            remaining = AUTO_CONNECT_INTERVAL - (time.monotonic() - loop_started)
            if remaining > 0:
                time.sleep(remaining)  # дожидаемся остаток периода, чтобы выдержать тайминг 200 мс

        ser.close()

    # ------ вспомогательные функции ---------------------------------
    @staticmethod
    def _calc_crc(data: bytes) -> int:
        crc = 0xFFFF
        for ch in data:
            crc ^= ch
            for _ in range(8):
                if crc & 1:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return crc

    def _check_crc(self, packet: bytes) -> bool:
        recv = int.from_bytes(packet[-2:], byteorder="little")
        calc = self._calc_crc(packet[:-2])
        return recv == calc

    @staticmethod
    def _parse_regs(packet: bytes) -> dict:
        """Возвращает настройки, извлечённые из пакета."""
        # данные начинаются после трёх байт заголовка
        regs = [int.from_bytes(packet[3 + i * 2 : 5 + i * 2], byteorder="big") for i in range(6)]
        return {
            "baud": regs[0] * 100,
            "bits": regs[1],
            "parity": regs[2],
            "stop": regs[3],
            "usart_id": regs[5],
        }


class RegisterPoller(QObject):
    """Поток опроса регистров 31-38 Modbus-устройства (смещение +9)."""  # обновили описание из-за новой карты

    data_ready = Signal(list)
    error = Signal(str)
    connection_lost = Signal()

    def __init__(self, serial_port: serial.Serial, slave_id: int):
        super().__init__()
        self.serial_port = serial_port
        self.slave_id = slave_id
        self._running = True
        self._fail_count = 0

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                # формируем запрос на чтение 8 регистров начиная с нового адреса
                start_addr = REG_SENSOR_READ_START  # используем адрес с учётом смещения
                req = struct.pack(">BBHH", self.slave_id, 3, start_addr, REG_SENSOR_READ_COUNT)  # подставляем новые значения
                crc = AutoConnectWorker._calc_crc(req)
                self.serial_port.write(req + crc.to_bytes(2, "little"))
                resp = self.serial_port.read(21)
                if len(resp) < 21 or not self._check_crc(resp):
                    self._fail_count += 1
                    if self._fail_count >= 5:
                        self.connection_lost.emit()
                        break
                    time.sleep(1)
                    continue
                self._fail_count = 0
                regs = [int.from_bytes(resp[3 + i * 2 : 5 + i * 2], "big") for i in range(REG_SENSOR_READ_COUNT)]  # длину цикла тоже берём из константы
                self.data_ready.emit(regs)
            except serial.SerialException as exc:
                self.error.emit(str(exc))
                break
            time.sleep(1)

    def _check_crc(self, packet: bytes) -> bool:
        recv = int.from_bytes(packet[-2:], "little")
        calc = AutoConnectWorker._calc_crc(packet[:-2])
        return recv == calc


class FirmwareUpdateWorker(QObject):
    """\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u041f\u041e \u0432 \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e\u043c \u043f\u043e\u0442\u043e\u043a\u0435."""

    progress = Signal(int, str)
    finished = Signal(bool)

    def __init__(self, serial_port: serial.Serial, slave_id: int, filename: str):
        super().__init__()
        self.serial_port = serial_port
        self.slave_id = slave_id
        self.filename = filename
        self._running = True

    def stop(self):
        self._running = False

    # --- \u043f\u0440\u043e\u0446\u0435\u0441\u0441 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f ----------------------
    def run(self):
        try:
            with open(self.filename, "rb") as f:
                firmware_data = f.read()
        except Exception:
            self.finished.emit(False)
            return

        total_packets = math.ceil(len(firmware_data) / MAX_PAYLOAD_SIZE)

        # ---- 0x2B ----
        if not self._send_start():
            self.finished.emit(False)
            return

        time.sleep(7)

        # ---- \u0441\u043c\u0435\u043d\u0430 \u043d\u0430 \u0434\u0435\u0444\u043e\u043b\u0442\u043d\u044b\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 ----
        orig_cfg = (
            self.serial_port.baudrate,
            self.serial_port.bytesize,
            self.serial_port.parity,
            self.serial_port.stopbits,
        )
        try:
            # \u043f\u043e\u0441\u043b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b 0x2B
            # \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u0435\u043c \u043f\u043e\u0440\u0442 \u043d\u0430 \u043e\u0431\u0449\u0438\u0435
            # \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0434\u043b\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438 0x2A
            self.serial_port.baudrate = DEFAULT_BAUDRATE
            self.serial_port.bytesize = DEFAULT_BYTESIZE
            self.serial_port.parity = DEFAULT_PARITY
            self.serial_port.stopbits = DEFAULT_STOPBITS
        except serial.SerialException:
            self.finished.emit(False)
            return

        first_ack = False
        for i in range(total_packets):
            if not self._running:
                self.finished.emit(False)
                return
            chunk = firmware_data[i * MAX_PAYLOAD_SIZE : (i + 1) * MAX_PAYLOAD_SIZE]
            if not self._send_packet(i + 1, total_packets, chunk):
                # \u043e\u0448\u0438\u0431\u043a\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438
                self._restore_serial(orig_cfg)
                self.finished.emit(False)
                return

            if not first_ack:
                first_ack = True

            pct = int((i + 1) * 100 / total_packets)
            msg = "" if first_ack else "\u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430 \u043a \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044e"
            self.progress.emit(pct, msg)

        self._restore_serial(orig_cfg)
        self.finished.emit(True)

    # ------------------------------------------------------------------
    def _restore_serial(self, cfg):
        try:
            self.serial_port.baudrate = cfg[0]
            self.serial_port.bytesize = cfg[1]
            self.serial_port.parity = cfg[2]
            self.serial_port.stopbits = cfg[3]
        except serial.SerialException:
            pass

    def _send_start(self) -> bool:
        try:
            req = struct.pack(">BB", self.slave_id, FUNC_CODE_START)
            crc = AutoConnectWorker._calc_crc(req)
            self.serial_port.write(req + crc.to_bytes(2, "little"))
            resp = self.serial_port.read(8)
            return len(resp) >= 4
        except serial.SerialException:
            return False

    def _send_packet(self, idx: int, total: int, payload: bytes) -> bool:
        frame = bytearray()
        frame += struct.pack(">BB", BOOTLOADER_ID, FUNC_CODE_FIRMWARE)
        frame += idx.to_bytes(2, "big")
        frame += total.to_bytes(2, "big")
        frame += payload
        crc = AutoConnectWorker._calc_crc(frame)
        frame += crc.to_bytes(2, "little")

        for _ in range(MAX_RETRIES):
            try:
                self.serial_port.write(frame)
                resp = self.serial_port.read(8)
                if len(resp) >= 4:
                    return True
            except serial.SerialException:
                pass
            time.sleep(RETRY_DELAY)
        return False


class BootloaderUpdateWorker(QObject):
    """Обновление прошивки напрямую из загрузчика (только пакеты 0x2A)."""

    progress = Signal(int, str)
    finished = Signal(bool)

    def __init__(self, port_name: str, filename: str, slave_id: int = BOOTLOADER_ID):
        super().__init__()
        self.port_name = port_name
        self.filename = filename
        self.slave_id = slave_id
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            ser = serial.Serial(
                self.port_name,
                baudrate=DEFAULT_BAUDRATE,
                bytesize=DEFAULT_BYTESIZE,
                parity=DEFAULT_PARITY,
                stopbits=DEFAULT_STOPBITS,
                timeout=1,
            )
        except serial.SerialException:
            self.finished.emit(False)
            return

        try:
            with open(self.filename, "rb") as f:
                firmware_data = f.read()
        except Exception:
            ser.close()
            self.finished.emit(False)
            return

        total_packets = math.ceil(len(firmware_data) / MAX_PAYLOAD_SIZE)
        first_ack = False
        for i in range(total_packets):
            if not self._running:
                ser.close()
                self.finished.emit(False)
                return

            chunk = firmware_data[i * MAX_PAYLOAD_SIZE : (i + 1) * MAX_PAYLOAD_SIZE]
            if not self._send_packet(ser, i + 1, total_packets, chunk):
                ser.close()
                self.finished.emit(False)
                return

            if not first_ack:
                first_ack = True

            pct = int((i + 1) * 100 / total_packets)
            msg = "" if first_ack else "подготовка к обновлению"
            self.progress.emit(pct, msg)

        ser.close()
        self.finished.emit(True)

    def _send_packet(self, ser: serial.Serial, idx: int, total: int, payload: bytes) -> bool:
        frame = bytearray()
        frame += struct.pack(">BB", self.slave_id, FUNC_CODE_FIRMWARE)
        frame += idx.to_bytes(2, "big")
        frame += total.to_bytes(2, "big")
        frame += payload
        crc = AutoConnectWorker._calc_crc(frame)
        frame += crc.to_bytes(2, "little")

        for _ in range(MAX_RETRIES):
            try:
                ser.write(frame)
                resp = ser.read(8)
                if len(resp) >= 4:
                    return True
            except serial.SerialException:
                pass
            time.sleep(RETRY_DELAY)
        return False

class UMVH(QMainWindow):


    SWAP_1_2_ENABLED = True


    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.setWindowIcon(QIcon(":/icons/app"))
        # при старте показываем страницу выбора файла
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_11)

        # --- фикс скругления выпадающих списков --------------------------
        for combo in self.findChildren(QComboBox):
            # (опционально) делаем popup дочерним, а не отдельным окном
            combo.setStyleSheet(combo.styleSheet() + "combobox-popup: 0;")

            view   = combo.view()          # QListView
            popup  = view.window()         # QComboBoxPrivateContainer (QFrame)

            # 1) убираем системную рамку и тень
            popup.setWindowFlags(Qt.Popup |
                                 Qt.FramelessWindowHint |
                                 Qt.NoDropShadowWindowHint)

            # 2) разрешаем прозрачный фон, чтобы радиус «обрезал» углы
            popup.setAttribute(Qt.WA_TranslucentBackground)

            # 3) задаём стиль контейнеру + самому списку
            popup.setStyleSheet("""
                /* сам контейнер -----------------------------------------*/
                QFrame { border:none; background:transparent; border-radius:12px; }

                /* список внутри (можно скопировать ваш QSS) ------------*/
                QListView {
                    border:1px solid #c8c8c8;
                    border-radius:12px;
                    padding:4px;
                    background:#ffffff;
                    outline:0;
                }
                QListView::item          { padding:6px 12px; border-radius:6px; }
                QListView::item:hover    { background:#f0f0f0; }
                QListView::item:selected { background:#2d97ff; color:#ffffff; }
            """)

        # --- загрузка списка COM-портов ---------------------------------
        self.selected_port = None  # переменная для хранения выбранного порта
        self.serial_config = {}
        self.serial_port = None
        self.worker_thread = None
        self.worker = None
        self.poll_thread = None
        self.poller = None
        self.update_thread = None
        self.updater = None
        self.bl_update_thread = None
        self.bl_updater = None
        self._calibration_reg_value = 0  # локальное хранение последнего значения регистра 0x001A

        # словарь ячеек таблицы датчиков для быстрого доступа
        self.sensor_cells = {
            row: {
                0: getattr(self.ui, f"s{row}s0x00"),
                1: getattr(self.ui, f"s{row}s0x01"),
                2: getattr(self.ui, f"s{row}s0x02"),
                4: getattr(self.ui, f"s{row}s0x04"),
            }
            for row in range(1, 9)
        }
        # словари для отслеживания изменений настроек
        self._saved_regs: dict[int, int] = {}
        self._changed_regs: dict[int, int] = {}
        self.populate_com_ports()
        # реагируем на смену выбора пользователем
        self.ui.comboBox_11.currentTextChanged.connect(self.on_port_selected)

        # --- обычная логика вашего приложения ----------------------------
        try:
            self.ui.stackedWidget.setCurrentWidget(self.ui.page)
        except AttributeError:
            self.ui.stackedWidget.setCurrentIndex(0)

        # переход в режим автоподключения
        self.ui.pushButton.clicked.connect(self.start_auto_connect)
        self.ui.pushButton_2.clicked.connect(lambda: self.switch_to(self.ui.page_3))

        # кнопка "Назад" на page_3
        self.ui.pushButton_3.clicked.connect(lambda: self.switch_to(self.ui.page))
        # кнопка "Подключиться" на page_3
        self.ui.pushButton_11.clicked.connect(self.manual_connect)
        # кнопка "Назад" на page_2
        self.ui.pushButton_5.clicked.connect(self.stop_auto_connect)

        # обработчики изменений на page_4
        self.ui.pushButton_4.clicked.connect(self.apply_settings)
        self.ui.comboBox_10.currentTextChanged.connect(self._on_setting_changed)
        self.ui.comboBox_10.currentTextChanged.connect(self._comboBox10_changed)
        self.ui.comboBox_9.currentIndexChanged.connect(self._on_setting_changed)
        self.ui.spinBox_7.valueChanged.connect(self._on_setting_changed)
        self.ui.spinBox_6.valueChanged.connect(self._on_setting_changed)
        self.ui.spinBox_5.valueChanged.connect(self._on_setting_changed)
        self.ui.spinBox_3.valueChanged.connect(self._on_setting_changed)
        self.ui.comboBox_5.currentTextChanged.connect(self._on_setting_changed)
        self.ui.comboBox_6.currentTextChanged.connect(self._on_setting_changed)
        self.ui.comboBox_7.currentIndexChanged.connect(self._on_setting_changed)
        self.ui.comboBox_8.currentTextChanged.connect(self._on_setting_changed)
        self.ui.spinBox_2.valueChanged.connect(self._on_setting_changed)
        self.ui.OS_update.clicked.connect(self.select_firmware_file)
        self.ui.pushButton_7.clicked.connect(self.select_bootloader_file)
        self.ui.comboBox_12.currentIndexChanged.connect(self._on_calibration_mode_changed)
        self.ui.pushButton_8.clicked.connect(self._send_calibration_y1)
        self.ui.pushButton_9.clicked.connect(self._send_calibration_y2)

        # устанавливаем состояние полей в зависимости от comboBox_10
        self._comboBox10_changed(self.ui.comboBox_10.currentText())
        self._apply_calibration_mode_ui(self.ui.comboBox_12.currentIndex())

    def switch_to(self, page_widget):
        self.ui.stackedWidget.setCurrentWidget(page_widget)
        if page_widget is self.ui.page_4:
            # при открытии page_4 показываем страницу выбора файла
            self.ui.stackedWidget_2.setCurrentWidget(self.ui.page_6)

    def populate_com_ports(self):
        """Заполняем comboBox_11 списком доступных COM портов."""
        ports = serial.tools.list_ports.comports()

        # сортируем список по возрастанию индекса, чтобы последний был максимальным
        def port_index(p):
            # выдёргиваем цифры из названия, например COM9 -> 9
            nums = "".join(filter(str.isdigit, p.device))
            return int(nums) if nums else -1

        ports = sorted(ports, key=port_index)

        self.ui.comboBox_11.clear()
        for port in ports:
            # добавляем имя устройства, например 'COM3'
            self.ui.comboBox_11.addItem(port.device)

        if ports:
            # выбираем порт с наибольшим индексом
            self.selected_port = ports[-1].device
            self.ui.comboBox_11.setCurrentText(self.selected_port)
        else:
            self.selected_port = None

    def on_port_selected(self, port_name: str):
        """Слот сохранения выбранного пользователем порта."""
        self.selected_port = port_name

    def _map_port_ui_to_device(self, port: int) -> int:
        """UI→Device: при выборе порта в UI возвращаем фактический порт на устройстве."""
        if not self.SWAP_1_2_ENABLED:
            return port
        return 2 if port == 1 else 1 if port == 2 else port

    def _map_port_device_to_ui(self, port: int) -> int:
        """Device→UI: преобразуем номер порта из устройства в отображаемый в интерфейсе."""
        if not self.SWAP_1_2_ENABLED:
            return port
        return 2 if port == 1 else 1 if port == 2 else port

    @contextmanager
    def _block_widget_signals(self, *widgets):
        """Временное отключение сигналов заданных виджетов."""
        states: list[bool] = []
        try:
            for widget in widgets:
                if widget is None:
                    states.append(False)
                    continue
                states.append(widget.blockSignals(True))
            yield
        finally:
            for widget, state in zip(widgets, states):
                if widget is None:
                    continue
                widget.blockSignals(state)

    def _swap_regs_for_ui(self, regs: list[int]) -> list[int]:
        """Device→UI: перед отображением меняем местами строки портов 1↔2."""
        if not self.SWAP_1_2_ENABLED:
            return regs
        if len(regs) >= 2:
            regs = regs.copy()
            regs[0], regs[1] = regs[1], regs[0]  # 0→порт1, 1→порт2
        return regs

    # --- работа с калибровкой --------------------------------------------
    def _apply_calibration_mode_ui(self, mode: int):
        """Переключаем страницу с набором точек калибровки."""
        if mode == 0:
            self.ui.stackedWidget_4.setCurrentWidget(self.ui.page_17)  # режим с двумя точками (Y1/Y2)
        else:
            self.ui.stackedWidget_4.setCurrentWidget(self.ui.page_16)  # режим с четырьмя точками (X1/Y1/X2/Y2)

    def _find_sensor_index(self, sensor_code: int) -> int:
        """Находим индекс пункта comboBox_9 по коду датчика."""
        prefix = f"0x{sensor_code & 0xF:02X}"
        for idx in range(self.ui.comboBox_9.count()):
            if self.ui.comboBox_9.itemText(idx).startswith(prefix):
                return idx
        return -1

    def _compose_calibration_register(self, mode: int | None = None) -> int:
        """Формируем значение регистра 0x001A из текущих настроек."""
        if mode is None:
            mode = 1 if self.ui.comboBox_12.currentIndex() == 1 else 0
        port_text = self.ui.comboBox_10.currentText()
        if port_text.isdigit():
            port_dev = self._map_port_ui_to_device(int(port_text))
        else:
            port_dev = (self._calibration_reg_value >> 4) & 0xF  # используем сохранённое значение
        try:
            sensor_code = int(self.ui.comboBox_9.currentText().split()[0], 16) & 0xF
        except ValueError:
            sensor_code = self._calibration_reg_value & 0xF
        return ((mode & 0x1) << 15) | ((port_dev & 0xF) << 4) | sensor_code

    def _ensure_calibration_register(self, desired: int | None = None) -> bool:
        """Проверяем, что регистр с режимом/портом/датчиком актуален."""
        if not self.serial_port:
            return False

        # соберём желаемое значение, если его не передали явно
        if desired is None:
            desired = self._compose_calibration_register()

        # перечитываем регистр, чтобы убедиться что устройство хранит те же параметры
        actual = self._read_register(REG_PORT_SENSOR_BIND)
        if actual is None:
            return False

        self._calibration_reg_value = actual
        if actual == desired:
            return True  # ничего отправлять не нужно — значения совпадают

        # если обнаружили расхождение — отправляем новое значение и сохраняем его локально
        if not self._write_register(REG_PORT_SENSOR_BIND, desired):
            return False

        self._calibration_reg_value = desired
        self._saved_regs[REG_PORT_SENSOR_BIND] = desired
        self._changed_regs.pop(REG_PORT_SENSOR_BIND, None)
        return True

    def _refresh_calibration_register(self, update_saved: bool = False) -> int | None:
        """Читаем регистр 0x001A и обновляем связанные элементы интерфейса."""
        value = self._read_register(REG_PORT_SENSOR_BIND)
        if value is None:
            return None
        self._calibration_reg_value = value
        mode = (value >> 15) & 0x1
        port_dev = (value >> 4) & 0xF
        sensor_code = value & 0xF

        with self._block_widget_signals(self.ui.comboBox_12):
            self.ui.comboBox_12.setCurrentIndex(mode if mode in (0, 1) else 0)
        self._apply_calibration_mode_ui(self.ui.comboBox_12.currentIndex())

        ui_port = self._map_port_device_to_ui(port_dev)
        with self._block_widget_signals(self.ui.comboBox_10):
            if ui_port and self.ui.comboBox_10.findText(str(ui_port)) != -1:
                self.ui.comboBox_10.setCurrentText(str(ui_port))
            else:
                self.ui.comboBox_10.setCurrentIndex(0)

        sensor_idx = self._find_sensor_index(sensor_code)
        if sensor_idx != -1:
            with self._block_widget_signals(self.ui.comboBox_9):
                self.ui.comboBox_9.setCurrentIndex(sensor_idx)
        self._comboBox10_changed(self.ui.comboBox_10.currentText())

        if update_saved:
            self._saved_regs[REG_PORT_SENSOR_BIND] = value
            self._changed_regs.pop(REG_PORT_SENSOR_BIND, None)
        return value

    def _refresh_calibration_points(self, update_saved: bool = False):
        """Обновляем значения точек калибровки из устройства."""
        mapping: list[tuple[int, tuple]] = [
            (REG_CAL_POINT_X1, (self.ui.spinBox_7,)),
            (REG_CAL_POINT_Y1, (self.ui.spinBox_6, self.ui.spinBox_8)),
            (REG_CAL_POINT_X2, (self.ui.spinBox_5,)),
            (REG_CAL_POINT_Y2, (self.ui.spinBox_3, self.ui.spinBox_9)),
        ]
        for addr, widgets in mapping:
            value = self._read_register(addr)
            if value is None:
                continue
            with self._block_widget_signals(*widgets):
                for widget in widgets:
                    widget.setValue(value)
            if update_saved:
                self._saved_regs[addr] = value
                self._changed_regs.pop(addr, None)

    def _has_negative_interpolation(self, x1: int, x2: int, y1: int, y2: int) -> bool:
        """Возвращает True, если точки задают отрицательный наклон."""
        return x1 > x2 or y1 > y2

    def _initialize_calibration_ui(self):
        """Читаем регистры калибровки при подключении и показываем актуальные данные."""
        if not self.serial_port:
            return
        self._refresh_calibration_register()
        self._refresh_calibration_points()

    def _on_calibration_mode_changed(self, index: int):
        """Обработчик переключения режима калибровки (2 или 4 точки)."""
        self._apply_calibration_mode_ui(index)
        if not self.serial_port:
            return
        new_value = self._compose_calibration_register(mode=index)
        if new_value == self._calibration_reg_value:
            return
        if not self._write_register(REG_PORT_SENSOR_BIND, new_value):
            self._handle_comm_error()
            return
        self._calibration_reg_value = new_value
        self._saved_regs[REG_PORT_SENSOR_BIND] = new_value
        self._changed_regs.pop(REG_PORT_SENSOR_BIND, None)
        self._refresh_calibration_register()

    def _send_calibration_point(self, register: int, value: int, widgets: tuple):
        """Отправляем значение точки калибровки и обновляем связанные поля."""
        if not self.serial_port:
            return
        # перед отправкой точки убеждаемся, что слейв знает выбранный режим/порт/датчик
        if not self._ensure_calibration_register():
            self._handle_comm_error()
            return
        if not self._write_register(register, value):
            self._handle_comm_error()
            return
        read_back = self._read_register(register)
        if read_back is not None:
            value = read_back
        with self._block_widget_signals(*widgets):
            for widget in widgets:
                widget.setValue(value)
        self._saved_regs[register] = value
        self._changed_regs.pop(register, None)

    def _send_calibration_y1(self):
        """Записываем точку Y1 в устройство (режим 2 точки)."""
        widgets = (self.ui.spinBox_6, self.ui.spinBox_8)
        self._send_calibration_point(REG_CAL_POINT_Y1, self.ui.spinBox_8.value(), widgets)

    def _send_calibration_y2(self):
        """Записываем точку Y2 в устройство (режим 2 точки)."""
        widgets = (self.ui.spinBox_3, self.ui.spinBox_9)
        self._send_calibration_point(REG_CAL_POINT_Y2, self.ui.spinBox_9.value(), widgets)
    # ------------------------------------------------------------------
    def start_auto_connect(self):
        """Запуск автоподключения и переход на страницу ожидания."""
        self._set_autoconnect_defaults()  # при входе на страницу выставляем базовые параметры 115200 8N1
        self.switch_to(self.ui.page_2)
        if not self.selected_port:
            return

        # создаём рабочий поток для приёма пакета
        self.worker_thread = QThread()
        self.worker = AutoConnectWorker(self.selected_port)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._auto_connect_finished)
        self.worker.error.connect(self._auto_connect_error)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.start()

    def _set_autoconnect_defaults(self):
        """Выставляем значения виджетов автоподключения на 115200 8N1."""
        idx_baud = self.ui.comboBox.findText("115200")
        if idx_baud != -1:
            self.ui.comboBox.setCurrentIndex(idx_baud)  # скорость 115 200 бод

        idx_bits = self.ui.comboBox_2.findText("8")
        if idx_bits != -1:
            self.ui.comboBox_2.setCurrentIndex(idx_bits)  # 8 бит данных

        self.ui.comboBox_4.setCurrentIndex(0)  # без чётности (8N1)
        self.ui.comboBox_3.setCurrentIndex(0)  # один стоп-бит

    def _auto_connect_error(self, message: str):
        # при ошибке просто выводим её в текстовое поле
        self.ui.textBrowser_2.setText(message)

    def manual_connect(self):
        """Ручное подключение с выбранными настройками."""
        if not self.selected_port:
            return

        # получаем параметры из элементов управления
        try:
            baud = int(self.ui.comboBox.currentText())
        except ValueError:
            baud = DEFAULT_BAUDRATE
        bits = int(self.ui.comboBox_2.currentText())
        parity_idx = self.ui.comboBox_4.currentIndex()
        parity_val = {0: serial.PARITY_NONE, 1: serial.PARITY_ODD, 2: serial.PARITY_EVEN}.get(parity_idx, serial.PARITY_NONE)
        stop = 2 if self.ui.comboBox_3.currentIndex() == 1 else 1
        slave = self.ui.spinBox.value()

        # создаем временное соединение и проверяем ответ
        try:
            ser = serial.Serial(
                self.selected_port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS if bits == 8 else serial.SEVENBITS,
                parity=parity_val,
                stopbits=serial.STOPBITS_TWO if stop == 2 else serial.STOPBITS_ONE,
                timeout=1,
            )
        except serial.SerialException as exc:
            self.ui.textBrowser_2.setText(str(exc))
            return

        # формируем запрос чтения одного регистра для проверки связи
        req = struct.pack(">BBHH", slave, 3, REG_SENSOR_READ_START, 1)  # используем новый адрес регистра для проверки связи
        crc = AutoConnectWorker._calc_crc(req)
        ser.write(req + crc.to_bytes(2, "little"))
        resp = ser.read(7)

        valid = False
        if len(resp) == 7:
            recv_crc = int.from_bytes(resp[-2:], "little")
            calc_crc = AutoConnectWorker._calc_crc(resp[:-2])
            valid = recv_crc == calc_crc

        ser.close()

        if not valid:
            # если ответ не получен или CRC неверен
            self.ui.textBrowser_2.setText("\u041d\u0435\u0442 \u043e\u0442\u0432\u0435\u0442\u0430 \u043e\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430")
            return

        settings = {
            "baud": baud,
            "bits": bits,
            "parity": parity_idx,
            "stop": stop,
            "usart_id": slave,
        }
        # используем существующий метод для открытия порта и запуска опроса
        self._auto_connect_finished(settings)

    def _auto_connect_finished(self, settings: dict):
        """Сохранение полученных настроек и открытие порта."""
        self.serial_config = settings
        # пробуем открыть порт с полученными параметрами
        try:
            self.serial_port = serial.Serial(
                self.selected_port,
                baudrate=settings["baud"],
                bytesize=serial.EIGHTBITS if settings["bits"] == 8 else serial.SEVENBITS,
                parity={0: serial.PARITY_NONE, 1: serial.PARITY_ODD, 2: serial.PARITY_EVEN}.get(settings["parity"], serial.PARITY_NONE),
                stopbits=serial.STOPBITS_TWO if settings["stop"] == 2 else serial.STOPBITS_ONE,
                timeout=1,
            )
        except serial.SerialException as exc:
            self.ui.textBrowser_2.setText(str(exc))
            return

        self._fill_settings_ui()
        self._initialize_calibration_ui()
        self._capture_page4_settings()
        self.switch_to(self.ui.page_4)
        self.start_polling()

    def _fill_settings_ui(self):
        """Заполняем виджеты на page_4 полученными значениями."""
        cfg = self.serial_config
        self.ui.comboBox_5.setCurrentText(str(cfg.get("baud", "")))
        self.ui.comboBox_6.setCurrentText(str(cfg.get("bits", "")))
        parity_idx = {0: 0, 1: 1, 2: 2}.get(cfg.get("parity"), 0)
        self.ui.comboBox_7.setCurrentIndex(parity_idx)
        self.ui.comboBox_8.setCurrentText(str(cfg.get("stop", "")))
        if "usart_id" in cfg:
            self.ui.spinBox_2.setValue(cfg["usart_id"])

    def _capture_page4_settings(self):
        """Сохраняем текущие значения виджетов страницы."""
        self._saved_regs = self._get_current_regs()
        self._changed_regs.clear()

    def _on_setting_changed(self):
        """Обработчик изменения любых настроек на page_4."""
        current = self._get_current_regs()
        for reg, val in current.items():
            if self._saved_regs.get(reg) != val:
                self._changed_regs[reg] = val
            elif reg in self._changed_regs:
                del self._changed_regs[reg]

    def _comboBox10_changed(self, text: str):
        """Включает или выключает связанные поля при выборе порта."""
        disabled = not text.isdigit()
        widgets = [
            self.ui.comboBox_9,
            self.ui.spinBox_7,
            self.ui.spinBox_6,
            self.ui.spinBox_5,
            self.ui.spinBox_3,
        ]
        for w in widgets:
            w.setEnabled(not disabled)
            # лёгкая заливка для визуального отличия неактивного состояния
            w.setStyleSheet("background-color: rgb(235,235,235);" if disabled else "")

    def _get_current_regs(self) -> dict[int, int]:
        """Возвращает словарь регистр -> значение из UI."""
        try:
            sensor = int(self.ui.comboBox_9.currentText().split()[0], 16)
        except ValueError:
            sensor = 0
        port_text = self.ui.comboBox_10.currentText()
        regs = {
            REG_BAUD: int(self.ui.comboBox_5.currentText()) // 100,  # все ключи берём с учётом смещения
            REG_BITS: int(self.ui.comboBox_6.currentText()),
            REG_PARITY: self.ui.comboBox_7.currentIndex(),
            REG_STOP: int(self.ui.comboBox_8.currentText()),
            REG_USART_ID: self.ui.spinBox_2.value(),
        }
        # если выбран конкретный порт, добавляем связанные регистры
        if port_text.isdigit():
            port_ui = int(port_text)
            port_dev = self._map_port_ui_to_device(port_ui)  # учитываем обмен портов 1↔2
            mode_bit = 1 if self.ui.comboBox_12.currentIndex() == 1 else 0  # формируем бит режима (0 - 2 точки, 1 - 4 точки)
            sensor_code = sensor & 0xF  # оставляем только младшие 4 бита типа датчика
            regs.update({
                REG_PORT_SENSOR_BIND: (mode_bit << 15) | ((port_dev & 0xF) << 4) | sensor_code,
                REG_CAL_POINT_X1: self.ui.spinBox_7.value(),
                REG_CAL_POINT_Y1: self.ui.spinBox_6.value(),
                REG_CAL_POINT_X2: self.ui.spinBox_5.value(),
                REG_CAL_POINT_Y2: self.ui.spinBox_3.value(),
            })
        return regs

    def _apply_new_serial(self):
        """Применяем настройки USART после отправки."""
        cfg = {
            "baud": int(self.ui.comboBox_5.currentText()),
            "bits": int(self.ui.comboBox_6.currentText()),
            "parity": self.ui.comboBox_7.currentIndex(),
            "stop": int(self.ui.comboBox_8.currentText()),
            "usart_id": self.ui.spinBox_2.value(),
        }
        try:
            if self.serial_port:
                self.serial_port.close()
            self.serial_port = serial.Serial(
                self.selected_port,
                baudrate=cfg["baud"],
                bytesize=serial.EIGHTBITS if cfg["bits"] == 8 else serial.SEVENBITS,
                parity={0: serial.PARITY_NONE, 1: serial.PARITY_ODD, 2: serial.PARITY_EVEN}.get(cfg["parity"], serial.PARITY_NONE),
                stopbits=serial.STOPBITS_TWO if cfg["stop"] == 2 else serial.STOPBITS_ONE,
                timeout=1,
            )
        except serial.SerialException:
            self._handle_comm_error()
            return
        self.serial_config = cfg
        self._fill_settings_ui()
        self.stop_polling()
        self.start_polling()

    def stop_auto_connect(self):
        """Останавливаем поток автоподключения и возвращаемся на главную."""
        if self.worker:
            self.worker.stop()
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
        self.stop_polling()
        self.switch_to(self.ui.page)

    # --- опрос регистров ------------------------------------------------
    def start_polling(self):
        if not self.serial_port:
            return
        self.poll_thread = QThread()
        slave = self.serial_config.get("usart_id", 1)
        self.poller = RegisterPoller(self.serial_port, slave)
        self.poller.moveToThread(self.poll_thread)
        self.poll_thread.started.connect(self.poller.run)
        self.poller.data_ready.connect(self.update_sensor_table)
        self.poller.error.connect(self.poll_error)
        self.poller.connection_lost.connect(self._handle_comm_error)
        self.poll_thread.start()

    def stop_polling(self):
        if self.poller:
            self.poller.stop()
        if self.poll_thread:
            self.poll_thread.quit()
            self.poll_thread.wait()

    def poll_error(self, msg: str):
        # выводим ошибку чтения в текстовое поле на странице ожидания
        self.ui.textBrowser_2.setText(msg)

    def update_sensor_table(self, regs: list[int]):
        """Обновляем таблицу датчиков на странице (с учётом свопа 1↔2 для UI)."""
        regs = self._swap_regs_for_ui(regs)  # <<< ДОБАВЛЕНО

        """Обновляем таблицу датчиков на странице."""
        for row, value in enumerate(regs, start=1):
            cells = self.sensor_cells.get(row, {})
            for sensor, widget in cells.items():
                if value & (1 << sensor):
                    # отображаем "Задано" по центру ячейки
                    widget.setText("\u0417\u0430\u0434\u0430\u043d\u043e")
                    widget.setAlignment(Qt.AlignCenter)
                    widget.setStyleSheet("background-color:rgb(165, 168, 180);")
                else:
                    # очищаем текст и убираем оформление
                    widget.setText("")
                    widget.setAlignment(Qt.AlignCenter)
                    widget.setStyleSheet("")

    # --- \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u041f\u041e ---------------------------------
    def select_firmware_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select firmware", "", "Hex files (*.hex);;All files (*)")
        if filename:
            self.start_firmware_update(filename)

    def start_firmware_update(self, filename: str):
        if not self.serial_port:
            return
        self.stop_polling()
        self.ui.progressBar.setValue(0)
        self.ui.progressBar.setFormat("0% \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430 \u043a \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044e")
        self.ui.stackedWidget_2.setCurrentWidget(self.ui.page_7)

        self.update_thread = QThread()
        slave = self.serial_config.get("usart_id", self.ui.spinBox_2.value())
        # \u043f\u043e \u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\ю
        # \u043f\u043e\u0441\u044b\u043b\u0430\u0435\u043c \u0431\u043b\u043e\u043a\u0438
        # 0x2A \u043d\u0430 USART ID = 1
        self.updater = FirmwareUpdateWorker(self.serial_port, slave, filename)
        self.updater.moveToThread(self.update_thread)
        self.update_thread.started.connect(self.updater.run)
        self.updater.progress.connect(self.update_progress)
        self.updater.finished.connect(self.update_finished)
        self.updater.finished.connect(self.update_thread.quit)
        self.update_thread.start()

    def update_progress(self, percent: int, msg: str):
        fmt = f"{percent}%" + (f" {msg}" if msg else "")
        self.ui.progressBar.setFormat(fmt)
        self.ui.progressBar.setValue(percent)

    def update_finished(self, success: bool):
        if success:
            self.ui.stackedWidget_2.setCurrentWidget(self.ui.page_9)
            QTimer.singleShot(5000, self._finish_success)
        else:
            self.ui.stackedWidget_2.setCurrentWidget(self.ui.page_8)
            QTimer.singleShot(5000, self._finish_failure)

    def _finish_success(self):
        self.ui.stackedWidget_2.setCurrentWidget(self.ui.page_6)
        self.start_polling()

    def _finish_failure(self):
        self.switch_to(self.ui.page)
        self.start_polling()

    # --- обновление из загрузчика ---------------------------------
    def select_bootloader_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select firmware", "", "Hex files (*.hex);;All files (*)")
        if filename:
            self.start_bootloader_update(filename)

    def start_bootloader_update(self, filename: str):
        if not self.selected_port:
            return
        # останавливаем возможный опрос и закрываем порт
        self.stop_polling()
        if self.serial_port:
            self.serial_port.close()
            self.serial_port = None

        self.ui.progressBar_2.setValue(0)
        self.ui.progressBar_2.setFormat("0% подготовка к обновлению")
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_10)

        self.bl_update_thread = QThread()
        self.bl_updater = BootloaderUpdateWorker(self.selected_port, filename)
        self.bl_updater.moveToThread(self.bl_update_thread)
        self.bl_update_thread.started.connect(self.bl_updater.run)
        self.bl_updater.progress.connect(self.boot_update_progress)
        self.bl_updater.finished.connect(self.boot_update_finished)
        self.bl_updater.finished.connect(self.bl_update_thread.quit)
        self.bl_update_thread.start()

    def boot_update_progress(self, percent: int, msg: str):
        fmt = f"{percent}%" + (f" {msg}" if msg else "")
        self.ui.progressBar_2.setFormat(fmt)
        self.ui.progressBar_2.setValue(percent)

    def boot_update_finished(self, success: bool):
        if success:
            self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_12)
        else:
            self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_13)
        QTimer.singleShot(5000, self._boot_finish_reset)

    def _boot_finish_reset(self):
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_11)
        self.switch_to(self.ui.page)
        self.bl_updater = None
        self.bl_update_thread = None

    def apply_settings(self):
        """Отправляет изменённые настройки на устройство (в т.ч. только пароль)."""
        if not self.serial_port:
            return

        # 1) Берём все изменения со страницы
        regs = self._changed_regs.copy()

        # 2) Если режим "2 точки" — X1/X2 не отправляем
        if self.ui.comboBox_12.currentIndex() == 0:
            regs.pop(REG_CAL_POINT_X1, None)
            regs.pop(REG_CAL_POINT_X2, None)

        # 3) Пароль добавляем всегда, даже если других изменений нет
        pwd_text = self.ui.textEditSP.toPlainText().strip()
        if pwd_text:
            try:
                # int(..., 0) позволяет "1234" и "0x1234"
                pwd_val = int(pwd_text, 0)
                if 0 <= pwd_val <= 0xFFFF:
                    regs[REG_PASSWORD] = pwd_val
            except ValueError:
                # Неверный формат пароля — просто игнорируем
                pass

        # 4) Если после всего нечего отправлять — выходим
        if not regs:
            return

        # Проверка для режима с четырьмя точками: запрещаем отрицательный наклон
        if self.ui.comboBox_12.currentIndex() == 1:
            x1 = regs.get(REG_CAL_POINT_X1, self.ui.spinBox_7.value())
            x2 = regs.get(REG_CAL_POINT_X2, self.ui.spinBox_5.value())
            y1 = regs.get(REG_CAL_POINT_Y1, self.ui.spinBox_6.value())
            y2 = regs.get(REG_CAL_POINT_Y2, self.ui.spinBox_3.value())
            if self._has_negative_interpolation(x1, x2, y1, y2):
                QMessageBox.warning(
                    self,
                    "Некорректные точки",
                    "Нельзя отправлять точки калибровки с отрицательным наклоном (X1 ≤ X2 и Y1 ≤ Y2).",
                )
                return

        # 5) Если шлём пароль, а REG_PORT_SENSOR_BIND не меняем —
        #    сначала убедимся, что на устройстве актуальны режим/порт/датчик
        if REG_PASSWORD in regs and REG_PORT_SENSOR_BIND not in regs:
            desired = self._compose_calibration_register()
            if not self._ensure_calibration_register(desired):
                self._handle_comm_error()
                return
            # REG_PORT_SENSOR_BIND в общий цикл не добавляем — уже актуализировали

            # При режиме двух точек перед отправкой пароля дополнительно проверяем наклон по данным из устройства
            if (desired >> 15) & 0x1 == 0:
                points: dict[int, int] = {}
                for addr in (
                    REG_CAL_POINT_X1,
                    REG_CAL_POINT_X2,
                    REG_CAL_POINT_Y1,
                    REG_CAL_POINT_Y2,
                ):
                    value = self._read_register(addr)
                    if value is None:
                        self._handle_comm_error()
                        return
                    points[addr] = value
                if self._has_negative_interpolation(
                    points.get(REG_CAL_POINT_X1, 0),
                    points.get(REG_CAL_POINT_X2, 0),
                    points.get(REG_CAL_POINT_Y1, 0),
                    points.get(REG_CAL_POINT_Y2, 0),
                ):
                    QMessageBox.warning(
                        self,
                        "Некорректные точки",
                        "Пароль не отправлен: точки калибровки образуют отрицательный наклон.",
                    )
                    return

        # 6) Порядок записи регистров
        order = [
            REG_PORT_SENSOR_BIND,
            REG_CAL_POINT_X1, REG_CAL_POINT_Y1,
            REG_CAL_POINT_X2, REG_CAL_POINT_Y2,
            REG_BAUD, REG_BITS, REG_PARITY, REG_STOP, REG_USART_ID,
        ]
        for reg in order:
            if reg in regs:
                if not self._write_register(reg, regs[reg]):
                    self._handle_comm_error()
                    return

        # 7) Пароль — в самом конце, отдельно
        if REG_PASSWORD in regs:
            if not self._write_register(REG_PASSWORD, regs[REG_PASSWORD]):
                self._handle_comm_error()
                return

        # 8) Локальное состояние
        self._saved_regs.update(regs)
        self._changed_regs.clear()
        if any(r in regs for r in (REG_BAUD, REG_BITS, REG_PARITY, REG_STOP, REG_USART_ID)):
            self._apply_new_serial()

        # 9) Сброс UI
        self.ui.comboBox_10.setCurrentIndex(0)
        self.ui.comboBox_9.setCurrentIndex(0)
        self.ui.spinBox_7.setValue(0)
        self.ui.spinBox_6.setValue(0)
        self.ui.spinBox_5.setValue(0)
        self.ui.spinBox_3.setValue(0)
        self.ui.textEditSP.clear()

    def _read_register(self, addr: int) -> int | None:
        """Чтение одного регистра Modbus."""
        if not self.serial_port:
            return None
        try:
            slave = self.serial_config.get("usart_id", 1)
            req = struct.pack(">BBHH", slave, 3, addr, 1)
            crc = AutoConnectWorker._calc_crc(req)
            self.serial_port.write(req + crc.to_bytes(2, "little"))
            resp = self.serial_port.read(7)
            if len(resp) != 7:
                return None
            recv_crc = int.from_bytes(resp[-2:], "little")
            if recv_crc != AutoConnectWorker._calc_crc(resp[:-2]):
                return None
            return int.from_bytes(resp[3:5], "big")
        except serial.SerialException:
            return None

    def _write_register(self, addr: int, value: int) -> bool:
        """Запись одного регистра Modbus."""
        try:
            slave = self.serial_config.get("usart_id", 1)
            req = struct.pack(">BBHH", slave, 6, addr, value)
            crc = AutoConnectWorker._calc_crc(req)
            self.serial_port.write(req + crc.to_bytes(2, "little"))
            resp = self.serial_port.read(8)
            if len(resp) != 8:
                return False
            recv_crc = int.from_bytes(resp[-2:], "little")
            calc_crc = AutoConnectWorker._calc_crc(resp[:-2])
            return recv_crc == calc_crc
        except serial.SerialException:
            return False

    def _handle_comm_error(self):
        """Отображает страницу ошибки и возвращается на главную."""
        self.stop_polling()
        if self.update_thread:
            self.update_thread.quit()
            self.update_thread.wait()
        if self.bl_update_thread:
            self.bl_update_thread.quit()
            self.bl_update_thread.wait()
            self.bl_update_thread = None
            self.bl_updater = None
        if self.serial_port:
            self.serial_port.close()
        self.switch_to(self.ui.page_5)
        QTimer.singleShot(5000, lambda: self.switch_to(self.ui.page))

    def closeEvent(self, event):
        """Гарантируем остановку потоков при закрытии окна."""
        self.stop_polling()
        if self.update_thread:
            self.update_thread.quit()
            self.update_thread.wait()
        if self.bl_update_thread:
            self.bl_update_thread.quit()
            self.bl_update_thread.wait()
            self.bl_update_thread = None
            self.bl_updater = None
        if self.serial_port:
            self.serial_port.close()
        super().closeEvent(event)


def main():
    app    = QApplication(sys.argv)
    window = UMVH()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
