from calibre.gui2.actions import InterfaceAction
from calibre_plugins.ao3_fandom_scanner.dialog import ScannerDialog

class AO3FandomScanner(InterfaceAction):
    name = 'AO3 Metadata Scanner'
    action_spec = ('AO3 Metadata Scanner', None,
                   'Scan EPUBs for AO3 fandom metadata', None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_scanner)

    def show_scanner(self):
        d = ScannerDialog(self.gui)
        d.exec()
