import sys

from PySide6.QtCore    import Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QComboBox
import serial.tools.list_ports

from ui_main import Ui_MainWindow


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
        self.populate_com_ports()
        # реагируем на смену выбора пользователем
        self.ui.comboBox_11.currentTextChanged.connect(self.on_port_selected)

        # --- обычная логика вашего приложения ----------------------------
        try:
            self.ui.stackedWidget.setCurrentWidget(self.ui.page)
        except AttributeError:
            self.ui.stackedWidget.setCurrentIndex(0)

        self.ui.pushButton.clicked  .connect(lambda: self.switch_to(self.ui.page_2))
        self.ui.pushButton_2.clicked.connect(lambda: self.switch_to(self.ui.page_3))

        # кнопка "Назад" на page_3
        self.ui.pushButton_3.clicked.connect(lambda: self.switch_to(self.ui.page))
        # кнопка "Назад" на page_2
        self.ui.pushButton_5.clicked.connect(lambda: self.switch_to(self.ui.page))

    def switch_to(self, page_widget):
        self.ui.stackedWidget.setCurrentWidget(page_widget)

    def populate_com_ports(self):
        """Заполняем comboBox_11 списком доступных COM портов."""
        ports = serial.tools.list_ports.comports()
        self.ui.comboBox_11.clear()
        for port in ports:
            # добавляем имя устройства, например 'COM3'
            self.ui.comboBox_11.addItem(port.device)

        # если есть хотя бы один порт, запоминаем первый из списка
        if ports:
            self.selected_port = ports[0].device
        else:
            self.selected_port = None

    def on_port_selected(self, port_name: str):
        """Слот сохранения выбранного пользователем порта."""
        self.selected_port = port_name


def main():
    app    = QApplication(sys.argv)
    window = UMVH()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
