from calibre.customize import InterfaceActionBase


class AO3FandomScannerPlugin(InterfaceActionBase):
    name = 'AO3 Metadata Scanner'
    description = ('Preview AO3 fandom, relationship, series, NSFW, and Dead Dove metadata from EPUBs '
                   'and optionally populate #fandom, #ship, #nsfw, #dead_dove, and the built-in series field.')
    supported_platforms = {'windows', 'osx', 'linux'}
    author = 'DracoBiblio'
    version = (0, 3, 6, 44)
   
    minimum_calibre_version = (0, 5, 0, 0)
    actual_plugin = 'calibre_plugins.ao3_fandom_scanner.ui:AO3FandomScanner'