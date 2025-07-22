import sys
from PySide6.QtWidgets import QApplication, QMainWindow

from ui_main import Ui_MainWindow

class UMVH(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        # show the first page by default
        try:
            self.ui.stackedWidget.setCurrentWidget(self.ui.page)
        except AttributeError:
            # fallback: index 0
            self.ui.stackedWidget.setCurrentIndex(0)
        # connect buttons to change pages
        self.ui.pushButton.clicked.connect(lambda: self.switch_to(self.ui.page_2))
        self.ui.pushButton_2.clicked.connect(lambda: self.switch_to(self.ui.page_3))

    def switch_to(self, page_widget):
        """Switch stacked widget to the given page."""
        self.ui.stackedWidget.setCurrentWidget(page_widget)


def main():
    app = QApplication(sys.argv)
    window = UMVH()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
