import sys

from PySide6.QtWidgets import QApplication, QMainWindow

from ui_main import  Ui_MainWindow

class UMVH(QMainWindow):
    def __init__(self):
        super(UMVH, self).__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)