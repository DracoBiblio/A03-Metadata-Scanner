from calibre.customize import InterfaceActionBase


class AO3FandomScannerPlugin(InterfaceActionBase):
    name = 'AO3 Metadata Scanner'
    description = ('Preview AO3 fandom, relationship, and series metadata from EPUBs '
                   'and optionally populate #fandom, #ship, and the built-in series field.')
    supported_platforms = {'windows', 'osx', 'linux'}
    author = 'DracBiblio'
    version = (0, 3, 3 )
    minimum_calibre_version = (9, 18, 0)
    actual_plugin = 'calibre_plugins.ao3_fandom_scanner.ui:AO3FandomScanner'
