from calibre.gui2.actions import InterfaceAction
from calibre_plugins.ao3_fandom_scanner.dialog import ScannerDialog

class AO3FandomScanner(InterfaceAction):
    name = 'Fanfiction Metadata Scanner'
    action_spec = ('Fanfiction Metadata Scanner', None,
                   'Scan books for fanfiction metadata', None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_scanner)

    def show_scanner(self):
        d = ScannerDialog(self.gui)
        d.exec()


