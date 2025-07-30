import sys

from PySide6.QtCore    import Qt, QObject, Signal, QThread
from PySide6.QtWidgets import QApplication, QMainWindow, QComboBox, QFileDialog
from PySide6.QtCore import QTimer
import math
import serial
import serial.tools.list_ports
import struct
import time

from ui_main import Ui_MainWindow

# --- константы для обновления ---
FUNC_CODE_FIRMWARE = 0x2A
FUNC_CODE_START    = 0x2B

MAX_PACKET_SIZE    = 91
HEADER_SIZE        = 5
CRC_SIZE           = 2
MAX_PAYLOAD_SIZE   = MAX_PACKET_SIZE - HEADER_SIZE - CRC_SIZE

MAX_RETRIES        = 3
RETRY_DELAY        = 1

# --- настройки USART для автоподключения и отправки 0x2A ---------
# используются при приёме пакета автоподключения и во время
# обновления прошивки
DEFAULT_BAUDRATE = 56000
DEFAULT_BYTESIZE = serial.EIGHTBITS
DEFAULT_PARITY   = serial.PARITY_NONE
DEFAULT_STOPBITS = serial.STOPBITS_ONE


class PacketSender:
    """Mixin with common packet sending logic."""

    def _send_packet(self, idx: int, total: int, payload: bytes) -> bool:
        frame = bytearray()
        frame += struct.pack(">BB", self.slave_id, FUNC_CODE_FIRMWARE)
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
            except (serial.SerialException, OSError):
                pass
            time.sleep(RETRY_DELAY)
        return False


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
                timeout=1,
            )
        except serial.SerialException as exc:
            self.error.emit(str(exc))
            return

        while self._running:
            data = ser.read(17)
            if len(data) < 17:
                continue

            if self._check_crc(data):
                settings = self._parse_regs(data)
                ser.close()
                self.finished.emit(settings)
                return

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
    """Поток опроса регистров 22-29 Modbus-устройства."""

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
                # формируем запрос на чтение 8 регистров начиная с адреса 22
                req = struct.pack(">BBHH", self.slave_id, 3, 22, 8)
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
                regs = [int.from_bytes(resp[3 + i * 2 : 5 + i * 2], "big") for i in range(8)]
                self.data_ready.emit(regs)
            except serial.SerialException as exc:
                self.error.emit(str(exc))
                break
            time.sleep(1)

    def _check_crc(self, packet: bytes) -> bool:
        recv = int.from_bytes(packet[-2:], "little")
        calc = AutoConnectWorker._calc_crc(packet[:-2])
        return recv == calc


class FirmwareUpdateWorker(QObject, PacketSender):
    """\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u041f\u041e \u0432 \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e\u043c \u043f\u043e\u0442\u043e\u043a\u0435."""

    progress = Signal(int, str)
    finished = Signal(bool)
    error = Signal()

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
            self.error.emit()
            self.finished.emit(False)
            return

        total_packets = math.ceil(len(firmware_data) / MAX_PAYLOAD_SIZE)

        # ---- 0x2B ----
        if not self._send_start():
            self.error.emit()
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
            self.error.emit()
            self.finished.emit(False)
            return

        first_ack = False
        for i in range(total_packets):
            if not self._running:
                self.error.emit()
                self.finished.emit(False)
                return
            chunk = firmware_data[i * MAX_PAYLOAD_SIZE : (i + 1) * MAX_PAYLOAD_SIZE]
            if not self._send_packet(i + 1, total_packets, chunk):
                # \u043e\u0448\u0438\u0431\u043a\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438
                self._restore_serial(orig_cfg)
                self.error.emit()
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



class BootloaderUpdateWorker(QObject, PacketSender):
    """Обновление ПО напрямую из загрузчика (только пакеты 0x2A)."""

    progress = Signal(int, str)
    finished = Signal(bool)
    error = Signal()

    def __init__(self, port_name: str, slave_id: int, filename: str):
        super().__init__()
        self.port_name = port_name
        self.slave_id = slave_id
        self.filename = filename
        self.serial_port: serial.Serial | None = None
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            self.serial_port = serial.Serial(
                self.port_name,
                baudrate=DEFAULT_BAUDRATE,
                bytesize=DEFAULT_BYTESIZE,
                parity=DEFAULT_PARITY,
                stopbits=DEFAULT_STOPBITS,
                timeout=1,
            )
            with open(self.filename, "rb") as f:
                firmware_data = f.read()

            total_packets = math.ceil(len(firmware_data) / MAX_PAYLOAD_SIZE)
            first_ack = False
            for i in range(total_packets):
                if not self._running:
                    self.error.emit()
                    self.finished.emit(False)
                    return
                chunk = firmware_data[i * MAX_PAYLOAD_SIZE : (i + 1) * MAX_PAYLOAD_SIZE]
                if not self._send_packet(i + 1, total_packets, chunk):
                    self.error.emit()
                    self.finished.emit(False)
                    return
                if not first_ack:
                    first_ack = True
                pct = int((i + 1) * 100 / total_packets)
                msg = "" if first_ack else "подготовка к обновлению"
                self.progress.emit(pct, msg)
            self.finished.emit(True)
        except Exception:
            self.error.emit()
            self.finished.emit(False)
        finally:
            try:
                if self.serial_port:
                    self.serial_port.close()
            except (serial.SerialException, OSError):
                pass


class UMVH(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

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
        self.boot_thread = None
        self.boot_updater = None

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
        # начальная страница загрузчика
        try:
            self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_11)
        except AttributeError:
            self.ui.stackedWidget_3.setCurrentIndex(3)

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
        self.ui.pushButton_7.clicked.connect(self.select_boot_firmware_file)

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

    # ------------------------------------------------------------------
    def start_auto_connect(self):
        """Запуск автоподключения и переход на страницу ожидания."""
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
        req = struct.pack(">BBHH", slave, 3, 22, 1)
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

    def _get_current_regs(self) -> dict[int, int]:
        """Возвращает словарь регистр -> значение из UI."""
        try:
            sensor = int(self.ui.comboBox_9.currentText().split()[0], 16)
        except ValueError:
            sensor = 0
        try:
            port = int(self.ui.comboBox_10.currentText())
        except ValueError:
            port = 0
        regs = {
            17: (port << 8) | sensor,
            18: self.ui.spinBox_7.value(),
            19: self.ui.spinBox_6.value(),
            20: self.ui.spinBox_5.value(),
            21: self.ui.spinBox_3.value(),
            30: int(self.ui.comboBox_5.currentText()) // 100,
            31: int(self.ui.comboBox_6.currentText()),
            32: self.ui.comboBox_7.currentIndex(),
            33: int(self.ui.comboBox_8.currentText()),
            35: self.ui.spinBox_2.value(),
        }
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
        # \u043f\u043e \u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\ю
        # \u043f\u043e\u0441\u044b\u043b\u0430\u0435\u043c \u0431\u043b\u043e\u043a\u0438
        # 0x2A \u043d\u0430 USART ID = 1
        self.updater = FirmwareUpdateWorker(self.serial_port, 1, filename)
        self.updater.moveToThread(self.update_thread)
        self.update_thread.started.connect(self.updater.run)
        self.updater.progress.connect(self.update_progress)
        self.updater.error.connect(self._handle_comm_error)
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

    # --- обновление из загрузчика ----------------------------------
    def select_boot_firmware_file(self):
        """Выбор файла прошивки для загрузчика."""
        filename, _ = QFileDialog.getOpenFileName(self, "Select firmware", "", "Hex files (*.hex);;All files (*)")
        if filename:
            self.start_boot_firmware_update(filename)

    def start_boot_firmware_update(self, filename: str):
        if not self.selected_port:
            return

        # закрываем возможный открытый порт и останавливаем опрос
        self.stop_polling()
        if self.serial_port:
            self.serial_port.close()
            self.serial_port = None

        self.ui.progressBar_2.setValue(0)
        self.ui.progressBar_2.setFormat("0%")
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_10)

        self.boot_thread = QThread()
        self.boot_updater = BootloaderUpdateWorker(self.selected_port, 1, filename)
        self.boot_updater.moveToThread(self.boot_thread)
        self.boot_thread.started.connect(self.boot_updater.run)
        self.boot_updater.progress.connect(self.boot_update_progress)
        self.boot_updater.error.connect(self._handle_comm_error)
        self.boot_updater.finished.connect(self.boot_update_finished)
        self.boot_updater.finished.connect(self.boot_thread.quit)
        self.boot_thread.finished.connect(self.boot_updater.deleteLater)
        self.boot_thread.start()

    def boot_update_progress(self, percent: int, msg: str):
        fmt = f"{percent}%" + (f" {msg}" if msg else "")
        self.ui.progressBar_2.setFormat(fmt)
        self.ui.progressBar_2.setValue(percent)

    def boot_update_finished(self, success: bool):
        if self.boot_thread and self.boot_thread.isRunning():
            self.boot_thread.wait()

        if success:
            # при успешном обновлении
            # сразу возвращаем интерфейс
            # в исходное состояние
            self._boot_finish_success()
        else:
            self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_13)
            QTimer.singleShot(5000, self._boot_finish_failure)

        self.boot_thread = None
        self.boot_updater = None

    def _boot_finish_success(self):
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_11)
        # сбрасываем состояние приложения после успешного обновления
        self._reset_app()

    def _boot_finish_failure(self):
        # соединение потеряно или произошла ошибка
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_11)
        self._reset_app()

    def apply_settings(self):
        """Отправляет изменённые настройки на устройство."""
        if not self.serial_port or not self._changed_regs:
            return

        regs = self._changed_regs.copy()
        pwd_text = self.ui.textEditSP.toPlainText().strip()
        if pwd_text:
            try:
                regs[34] = int(pwd_text)
            except ValueError:
                pass

        order = [17, 18, 19, 20, 21, 30, 31, 32, 33, 35]
        for reg in order:
            if reg in regs:
                if not self._write_register(reg, regs[reg]):
                    self._handle_comm_error()
                    return
        if 34 in regs:
            if not self._write_register(34, regs[34]):
                self._handle_comm_error()
                return

        self._saved_regs.update(regs)
        self._changed_regs.clear()

        if any(r in regs for r in (30, 31, 32, 33, 35)):
            self._apply_new_serial()

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
        if self.updater:
            self.updater.stop()
        if self.update_thread:
            self.update_thread.quit()
            self.update_thread.wait()
        if self.boot_updater:
            self.boot_updater.stop()
        if self.boot_thread:
            self.boot_thread.quit()
            self.boot_thread.wait()
        if self.serial_port:
            self.serial_port.close()
        self.switch_to(self.ui.page_5)
        QTimer.singleShot(5000, lambda: self.switch_to(self.ui.page))

    def _reset_app(self):
        """Полный сброс состояния приложения."""
        # Останавливаем все потоки и закрываем открытые порты
        self.stop_polling()
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
            self.worker_thread = None
        self.worker = None
        if self.update_thread:
            self.update_thread.quit()
            self.update_thread.wait()
            self.update_thread = None
        self.updater = None
        if self.boot_updater:
            self.boot_updater.stop()
        if self.boot_thread:
            self.boot_thread.quit()
            self.boot_thread.wait()
            self.boot_thread = None
        self.boot_updater = None
        if self.serial_port:
            self.serial_port.close()
            self.serial_port = None

        # Сбрасываем внутренние переменные
        self.selected_port = None
        self.serial_config = {}
        self._saved_regs.clear()
        self._changed_regs.clear()

        # Обновляем список портов и возвращаем интерфейс в начальное состояние
        self.populate_com_ports()
        self.switch_to(self.ui.page)
        self.ui.stackedWidget_2.setCurrentWidget(self.ui.page_6)
        self.ui.stackedWidget_3.setCurrentWidget(self.ui.page_11)

    def closeEvent(self, event):
        """Гарантируем остановку потоков при закрытии окна."""
        self.stop_polling()
        if self.update_thread:
            self.update_thread.quit()
            self.update_thread.wait()
        if self.boot_updater:
            self.boot_updater.stop()
        if self.boot_thread:
            self.boot_thread.quit()
            self.boot_thread.wait()
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
