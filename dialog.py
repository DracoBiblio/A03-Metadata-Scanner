import re
from qt.core import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                     QTableWidget, QTableWidgetItem, QMessageBox, QHeaderView, QGroupBox,
                     Qt, QAbstractItemView, QTimer, QComboBox, QLineEdit, QCheckBox)
from functools import partial
from calibre_plugins.ao3_fandom_scanner.parser import (
    parse_epub, parse_safety_flags, parse_additional_tags,
    parse_pdf, parse_pdf_safety_flags, parse_pdf_additional_tags, parse_pdf_bundle,
    detect_file_content, parse_html, parse_html_safety_flags, parse_html_additional_tags,
    find_ao3_url,
)
from calibre_plugins.ao3_fandom_scanner.worker import LookupWorker
from calibre_plugins.ao3_fandom_scanner.firefox_session import get_ao3_cookie_header

# Existing Calibre columns are deliberately retained for compatibility.
FIELDS = [
    {'field': '#fandom', 'key': 'fandoms'},
    {'field': '#ship', 'key': 'ships'},
    {'field': 'series', 'key': 'series'},
    {'field': '#nsfw', 'key': 'nsfw'},
    {'field': '#dead_dove', 'key': 'dead_dove'},
    {'field': 'tags', 'key': 'tags'},
]
SCAN_BATCH_SIZE = 2

# New columns are preview-only in this first UI revision. They are not written
# to Calibre until matching parser support and configurable target columns exist.
COLUMN_LABELS = [
    'Update?', 'Book', 'Source', 'Metadata origin',
    'Fandom found', 'Fandom now',
    'Characters found', 'Relationships found', 'Ship now',
    'Series found', 'Series now',
    'Rating found', 'Warnings found', 'Genre found',
    'NSFW found', 'NSFW now',
    'Dead Dove found', 'Dead Dove now',
    'Tags found', 'Tags now', 'Scan result', 'Online AO3 result',
]


class ScannerDialog(QDialog):
    def __init__(self, gui):
        super().__init__(gui)
        self.gui = gui
        self.db = gui.current_db.new_api
        self.rows = []
        self.pending_ids = []
        self._scanning = False
        self._lookup_worker = None
        self.setWindowTitle('Fanfiction Metadata Scanner — Preview')
        self.resize(1600, 820)
        self.setMinimumSize(1120, 650)

        self._missing_fields = {
            spec['field'] for spec in FIELDS
            if spec['field'] not in self.db.field_metadata
        }

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Review fanfiction metadata found in your books. Preview does not '
            'change book data. Existing Calibre values are shown. Applying '
            'replaces existing values only when usable data was detected.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Scan controls
        scan_buttons = QHBoxLayout()
        self.scan_selected_btn = QPushButton('Scan selected books')
        self.scan_all_btn = QPushButton('Scan all books')
        scan_buttons.addWidget(QLabel('Format:'))
        self.format_combo = QComboBox()
        self.format_combo.addItems(['Auto (EPUB preferred)', 'EPUB only', 'PDF only'])
        self.format_combo.setToolTip(
            'Choose which format to scan when a book has both EPUB and PDF. '
            'Auto scans EPUB first, otherwise PDF.'
        )
        scan_buttons.addWidget(self.format_combo)

        scan_buttons.addWidget(QLabel('Source:'))
        self.source_combo = QComboBox()
        self.source_combo.addItems([
            'Auto-detect', 'Archive of Our Own', 'FanFiction.net',
            'FicHub', 'Other / Unknown'
        ])
        self.source_combo.setToolTip(
            'Source selection is currently informational. Automatic source '
            'identification and source-specific parsing will be added separately.'
        )
        scan_buttons.addWidget(self.source_combo)

        self.online_lookup = QCheckBox('Look up additional metadata online')
        self.online_lookup.setChecked(False)
        self.online_lookup.setToolTip(
            'When checked, an AO3 lookup pass can be started after the saved-file scan. '
            'Only books with a discovered AO3 work URL are queried. Requests are throttled. '
            'Only discovered AO3 work URLs are looked up. FicHub links are ignored; an original AO3 work URL inside a FicHub export can still be used.'
        )
        scan_buttons.addWidget(self.online_lookup)
        self.firefox_session = QCheckBox('Use Firefox AO3 session')
        self.firefox_session.setChecked(False)
        self.firefox_session.setToolTip('If enabled, read only AO3 cookies from your local Firefox profile for this lookup pass. Cookies are not saved by the plugin.')
        scan_buttons.addWidget(self.firefox_session)
        self.lookup_btn = QPushButton('Run AO3 lookup')
        self.lookup_btn.setEnabled(False)
        self.cancel_lookup_btn = QPushButton('Cancel lookup')
        self.cancel_lookup_btn.setEnabled(False)
        scan_buttons.addWidget(self.lookup_btn)
        scan_buttons.addWidget(self.cancel_lookup_btn)

        scan_buttons.addWidget(self.scan_selected_btn)
        scan_buttons.addWidget(self.scan_all_btn)
        scan_buttons.addStretch(1)
        self.close_btn = QPushButton('Close')
        scan_buttons.addWidget(self.close_btn)
        layout.addLayout(scan_buttons)

        apply_group = QGroupBox('Replace detected metadata in Calibre')
        apply_layout = QHBoxLayout(apply_group)
        apply_layout.setContentsMargins(8, 8, 8, 8)
        apply_layout.setSpacing(8)
        self.select_all_btn = QPushButton('Select all')
        self.select_none_btn = QPushButton('Select none')
        apply_layout.addWidget(self.select_all_btn)
        apply_layout.addWidget(self.select_none_btn)
        apply_layout.addStretch(1)

        self.apply_btns = {}
        self.tag_mode = QComboBox()
        self.tag_mode.addItems(['Merge with existing Tags', 'Replace existing Tags'])
        for spec in FIELDS:
            label = spec['field'].lstrip('#').replace('_', ' ').title()
            btn = QPushButton(f'Replace {label}')
            btn.setEnabled(False)
            if spec['field'] in self._missing_fields:
                btn.setToolTip(
                    f'The {spec["field"]} column does not exist. '
                    'Create it in Calibre preferences first.'
                )
            self.apply_btns[spec['field']] = btn
            apply_layout.addWidget(btn)

        apply_layout.addWidget(QLabel('Tags mode:'))
        apply_layout.addWidget(self.tag_mode)
        layout.addWidget(apply_group)

        filter_group = QGroupBox('Find and filter scanned books')
        filter_layout = QHBoxLayout(filter_group)
        filter_layout.addWidget(QLabel('Show:'))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems([
            'All scanned books',
            'Missing Fandom',
            'Missing Ship',
            'Missing Series',
            'Has NSFW = Yes',
            'Has NSFW = No',
            'Has Dead Dove = Yes',
            'Has Dead Dove = No',
            'Has any safety flag',
        ])
        filter_layout.addWidget(self.filter_combo)
        filter_layout.addWidget(QLabel('Search title or metadata:'))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            'Type to find a book, fandom, relationship, series, or tag…'
        )
        filter_layout.addWidget(self.search_box, 1)
        self.select_visible_btn = QPushButton('Select visible')
        self.deselect_visible_btn = QPushButton('Deselect visible')
        filter_layout.addWidget(self.select_visible_btn)
        filter_layout.addWidget(self.deselect_visible_btn)
        layout.addWidget(filter_group)

        self.table = QTableWidget(0, len(COLUMN_LABELS))
        self.table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setWordWrap(False)
        widths = [
            72, 230, 125, 125, 210, 190, 220, 230, 210, 180, 170,
            110, 180, 150, 105, 105, 125, 125, 280, 260, 250, 300
        ]
        for c, width in enumerate(widths):
            self.table.setColumnWidth(c, width)
        layout.addWidget(self.table)

        self.status = QLabel(
            'Choose books to scan. Saved-file scanning remains offline.'
        )
        layout.addWidget(self.status)

        self.scan_selected_btn.clicked.connect(self.scan_selected)
        self.scan_all_btn.clicked.connect(self.scan_all)
        for spec in FIELDS:
            self.apply_btns[spec['field']].clicked.connect(
                partial(self.apply_results, spec)
            )
        self.close_btn.clicked.connect(self.close)
        self.lookup_btn.clicked.connect(self.run_online_lookup)
        self.cancel_lookup_btn.clicked.connect(self.cancel_online_lookup)
        self.select_all_btn.clicked.connect(partial(self.set_all_checked, True))
        self.select_none_btn.clicked.connect(partial(self.set_all_checked, False))
        self.select_visible_btn.clicked.connect(partial(self.set_visible_checked, True))
        self.deselect_visible_btn.clicked.connect(partial(self.set_visible_checked, False))
        self.filter_combo.currentTextChanged.connect(self.apply_table_filter)
        self.search_box.textChanged.connect(self.apply_table_filter)

        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._scan_batch)

    # ---------- scanning ----------

    def scan_selected(self):
        ids = list(self.gui.current_view().get_selected_ids())
        if not ids:
            QMessageBox.information(
                self, 'Fanfiction Metadata Scanner',
                'Select one or more books in the library first.'
            )
            return
        self.scan_ids(ids)

    def scan_all(self):
        try:
            ids = list(self.db.search('format:EPUB or format:PDF'))
        except Exception:
            # db.formats() returns None for format-less books — guard with `or ()`.
            ids = [
                bid for bid in self.db.all_book_ids()
                if {'EPUB', 'PDF'}.intersection(self.db.formats(bid) or ())
            ]
        self.scan_ids(sorted(ids))

    def scan_ids(self, ids):
        self.rows = []
        self.table.setRowCount(0)
        mode = self.format_combo.currentText()
        if mode == 'EPUB only':
            wanted = {'EPUB'}
        elif mode == 'PDF only':
            wanted = {'PDF'}
        else:
            wanted = {'EPUB', 'PDF'}
        self.pending_ids = [
            bid for bid in ids if wanted.intersection(self.db.formats(bid) or ())
        ]
        self._scanning = True
        self._set_buttons_enabled(False)
        self.status.setText(
            f'Scanning {len(self.pending_ids)} book(s) using {mode}…'
        )
        self._scan_timer.start(0)

    def _scan_batch(self):
        for bid in self.pending_ids[:SCAN_BATCH_SIZE]:
            self._scan_one(bid)
        del self.pending_ids[:SCAN_BATCH_SIZE]
        if self.pending_ids:
            self.status.setText(
                f'Scanning… {len(self.rows)} done, '
                f'{len(self.pending_ids)} remaining'
            )
        else:
            self._scan_timer.stop()
            self._scanning = False
            self._set_buttons_enabled(True)
            self.apply_table_filter()

    @staticmethod
    def _guess_source(path):
        """Conservative source hint from a saved file; not a verified parser."""
        try:
            with open(path, 'rb') as stream:
                sample = stream.read(512 * 1024).lower()
            if b'archiveofourown.org' in sample or b'archive of our own' in sample:
                return 'Archive of Our Own'
            if b'fanfiction.net' in sample:
                return 'FanFiction.net'
            if b'fichub.net' in sample or b'exported with the assistance of fichub' in sample:
                return 'FicHub'
        except Exception:
            pass
        return 'Other / Unknown'

    def _scan_one(self, bid):
        mi = self.db.get_metadata(bid)
        row = {
            'id': bid,
            'title': mi.title or '(untitled)',
            'result': '',
            'source': 'Other / Unknown',
            'origin': 'Saved file',
            'characters': None,
            'rating': None,
            'warnings': None,
            'genre': None,
            'author': getattr(mi, 'authors', None),
            'ao3_url': None,
            'online_result': 'Not run',
        }
        for spec in FIELDS:
            row[spec['key']] = None
            row['existing_' + spec['key']] = self._field_text(spec['field'], bid)

        # db.formats() returns None for format-less books — guard with `or ()`.
        formats = self.db.formats(bid) or ()
        mode = self.format_combo.currentText()
        if mode == 'EPUB only':
            preferred = 'EPUB' if 'EPUB' in formats else None
        elif mode == 'PDF only':
            preferred = 'PDF' if 'PDF' in formats else None
        else:
            preferred = 'EPUB' if 'EPUB' in formats else (
                'PDF' if 'PDF' in formats else None
            )

        path = self.db.format_abspath(bid, preferred) if preferred else None
        fmt = detect_file_content(path) if path else 'UNKNOWN'
        if not path:
            row['nsfw'], row['dead_dove'] = None, None
            row['tags'] = None
            row['result'] = fmt + ' file unavailable'
        else:
            row['source'] = self._guess_source(path)
            try:
                row['ao3_url'] = find_ao3_url(path)
                if fmt == 'PDF':
                    (fandoms, ships, series, row['tags'], result,
                     row['nsfw'], row['dead_dove']) = parse_pdf_bundle(path)
                elif fmt == 'HTML':
                    fandoms, ships, series, result = parse_html(path)
                    row['tags'] = parse_html_additional_tags(path)
                    row['nsfw'], row['dead_dove'] = parse_html_safety_flags(
                        path, row['tags']
                    )
                elif fmt == 'EPUB':
                    fandoms, ships, series, result = parse_epub(path)
                    row['tags'] = parse_additional_tags(path)
                    row['nsfw'], row['dead_dove'] = parse_safety_flags(
                        path, row['tags']
                    )
                else:
                    fandoms = ships = series = None
                    row['tags'] = None
                    row['nsfw'], row['dead_dove'] = None, None
                    result = 'Unsupported or unrecognized file contents'
                row['result'] = f'{fmt}: {result}'
            except Exception as e:
                fandoms = ships = series = None
                row['tags'] = None
                row['nsfw'], row['dead_dove'] = None, None
                row['result'] = f'Parse error: {e}'

            row['fandoms'], row['ships'], row['series'] = fandoms, ships, series

        # The source selector is a user-facing hint for the next parser stage.
        selected_source = self.source_combo.currentText()
        if selected_source != 'Auto-detect':
            row['source'] = selected_source

        self.rows.append(row)
        self._append_row_to_table(row)

    # ---------- table ----------

    def _field_text(self, field, bid):
        try:
            value = self.db.field_for(field, bid, default_value=())
        except Exception:
            return ''
        if isinstance(value, str):
            return value
        # Series fields (built-in and custom) return a Series value whose
        # index lives in a separate column; show the name like Calibre does.
        if hasattr(value, 'name'):
            return value.name
        return ', '.join(value or ())

    @staticmethod
    def _join(value):
        if not value:
            return ''
        if isinstance(value, str):
            return value
        return ', '.join(str(x) for x in value)

    def _row_display_values(self, row):
        return (
            '',
            row['title'],
            row.get('source') or '',
            row.get('origin') or '',
            self._join(row.get('fandoms')),
            row['existing_fandoms'],
            self._join(row.get('characters')),
            self._join(row.get('ships')),
            row['existing_ships'],
            self._join(row.get('series')),
            row['existing_series'],
            self._join(row.get('rating')),
            self._join(row.get('warnings')),
            self._join(row.get('genre')),
            row.get('nsfw') or '',
            row['existing_nsfw'],
            row.get('dead_dove') or '',
            row['existing_dead_dove'],
            self._join(row.get('tags')),
            row['existing_tags'],
            row.get('result') or '',
            row.get('online_result') or 'Not run',
        )

    def _append_row_to_table(self, row):
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, value in enumerate(self._row_display_values(row)):
            item = QTableWidgetItem(str(value or ''))
            if c == 0:
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled |
                    Qt.ItemFlag.ItemIsUserCheckable |
                    Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(Qt.CheckState.Checked)
                item.setText('')
                item.setToolTip('Include this book when applying metadata')
            elif c == 1:
                item.setData(Qt.ItemDataRole.UserRole, row['id'])
            elif c == 2:
                item.setToolTip(
                    'Source is a best-effort hint inferred from text in the saved file.'
                )
            elif c == 3:
                item.setToolTip(
                    'Saved-file values remain unchanged by online lookup. '
                    'Online AO3 results are shown in the final column only.'
                )
            self.table.setItem(r, c, item)

    def set_all_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(state)
        self._update_status()

    def set_visible_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            if self.table.isRowHidden(r):
                continue
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(state)
        self._update_status()

    def apply_table_filter(self, *args):
        mode = self.filter_combo.currentText()
        query = self.search_box.text().strip().casefold()
        for r, row in enumerate(self.rows):
            fandoms = row.get('fandoms') or []
            ships = row.get('ships') or []
            series = row.get('series') or []
            nsfw = (row.get('nsfw') or '').casefold()
            dead_dove = (row.get('dead_dove') or '').casefold()

            show = True
            if mode == 'Missing Fandom':
                show = not bool(fandoms)
            elif mode == 'Missing Ship':
                show = not bool(ships)
            elif mode == 'Missing Series':
                show = not bool(series)
            elif mode == 'Has NSFW = Yes':
                show = nsfw == 'yes'
            elif mode == 'Has NSFW = No':
                show = nsfw == 'no'
            elif mode == 'Has Dead Dove = Yes':
                show = dead_dove == 'yes'
            elif mode == 'Has Dead Dove = No':
                show = dead_dove == 'no'
            elif mode == 'Has any safety flag':
                show = nsfw == 'yes' or dead_dove == 'yes'

            if show and query:
                searchable = ' '.join(
                    str(x) for x in self._row_display_values(row)
                ).casefold()
                show = query in searchable
            self.table.setRowHidden(r, not show)
        self._update_status()

    def _checked_book_ids(self):
        ids = set()
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                book_item = self.table.item(r, 1)
                if book_item is not None:
                    ids.add(book_item.data(Qt.ItemDataRole.UserRole))
        return ids

    def _refresh_existing_fields(self):
        for row in self.rows:
            for spec in FIELDS:
                row['existing_' + spec['key']] = self._field_text(
                    spec['field'], row['id']
                )
        self.table.setRowCount(0)
        for row in self.rows:
            self._append_row_to_table(row)
        self.apply_table_filter()

    def _update_status(self):
        counts = [sum(1 for x in self.rows if x[s['key']]) for s in FIELDS]
        labels = ['#fandom', '#ship', '#series', '#nsfw', '#dead_dove', 'tags']
        summary = '; '.join(
            f'{n} with {lbl} data' for n, lbl in zip(counts, labels)
        )
        selected = len(self._checked_book_ids())
        visible = sum(
            1 for r in range(self.table.rowCount())
            if not self.table.isRowHidden(r)
        )
        self.status.setText(
            f'Checked {len(self.rows)} book record(s); {visible} visible; '
            f'{selected} selected for update; {summary}. '
            'Saved-file metadata remains separate from online AO3 results.'
        )

    def run_online_lookup(self):
        if not self.online_lookup.isChecked():
            QMessageBox.information(
                self, 'AO3 online lookup',
                'Check “Look up additional metadata online” first. No website is contacted unless you start this pass.'
            )
            return
        if self._lookup_worker is not None and self._lookup_worker.isRunning():
            return
        selected = self._checked_book_ids()
        items = [
            (row['id'], row['ao3_url']) for row in self.rows
            if row['id'] in selected and row.get('ao3_url')
        ]
        if not items:
            QMessageBox.information(
                self, 'AO3 online lookup',
                'No checked books have a discovered AO3 work URL. The lookup supports AO3 URLs only.'
            )
            return
        self.lookup_btn.setEnabled(False)
        self.cancel_lookup_btn.setEnabled(True)
        self.scan_selected_btn.setEnabled(False)
        self.scan_all_btn.setEnabled(False)
        for row in self.rows:
            if row['id'] in dict(items):
                row['online_result'] = 'Waiting…'
        self._refresh_table_preserving_selection()
        cookie_header = None
        if self.firefox_session.isChecked():
            cookie_header, cookie_message = get_ao3_cookie_header()
            if not cookie_header:
                self.lookup_btn.setEnabled(True)
                self.cancel_lookup_btn.setEnabled(False)
                self.scan_selected_btn.setEnabled(True)
                self.scan_all_btn.setEnabled(True)
                QMessageBox.warning(self, 'Firefox AO3 session', cookie_message + '\n\nYou can uncheck the option to try an anonymous lookup.')
                return
            self.status.setText('Using Firefox AO3 session; cookies remain in memory for this lookup pass…')
        else:
            self.status.setText(f'Looking up {len(items)} AO3 work(s) anonymously; at least 2 seconds between requests…')
        self._lookup_worker = LookupWorker(items, delay=2.0, cookie_header=cookie_header, parent=self)
        self._lookup_worker.book_done.connect(self._online_book_done)
        self._lookup_worker.finished_all.connect(self._online_lookup_finished)
        self._lookup_worker.start()

    def cancel_online_lookup(self):
        if self._lookup_worker is not None and self._lookup_worker.isRunning():
            self._lookup_worker.cancel()
            self.status.setText('Cancellation requested; the current AO3 request will finish first…')
            self.cancel_lookup_btn.setEnabled(False)

    def _online_book_done(self, bid, result):
        row = next((r for r in self.rows if r['id'] == bid), None)
        if row is None:
            return
        row['online_data'] = result.get('fields') or {}
        if result.get('ok'):
            fields = row['online_data']
            summary = []
            for label in ('fandoms', 'relationships', 'characters', 'rating', 'archive warnings', 'additional tags', 'series'):
                value = fields.get(label)
                if value:
                    if isinstance(value, list):
                        value = ', '.join(value)
                    summary.append(f'{label.title()}: {value}')
            row['online_result'] = 'AO3 website — ' + (' | '.join(summary) if summary else 'Metadata loaded')
        else:
            row['online_result'] = 'AO3 lookup failed: ' + (result.get('error') or 'No details returned')
        self._refresh_table_preserving_selection()

    def _online_lookup_finished(self, results):
        self.cancel_lookup_btn.setEnabled(False)
        self.scan_selected_btn.setEnabled(True)
        self.scan_all_btn.setEnabled(True)
        self.lookup_btn.setEnabled(any(r.get('ao3_url') for r in self.rows))
        ok = sum(1 for result in results.values() if result.get('ok'))
        self.status.setText(
            f'Online AO3 lookup finished: {ok} successful of {len(results)} attempted. '
            'Website results remain preview-only; saved-file values were not changed.'
        )
        self._lookup_worker = None
        self.apply_table_filter()

    def _refresh_table_preserving_selection(self):
        selected = self._checked_book_ids()
        self.table.setRowCount(0)
        for row in self.rows:
            self._append_row_to_table(row)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            book_item = self.table.item(r, 1)
            bid = book_item.data(Qt.ItemDataRole.UserRole) if book_item else None
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if bid in selected else Qt.CheckState.Unchecked)
        self.apply_table_filter()

    def _has_scan_results(self):
        return bool(self.rows)

    def _set_buttons_enabled(self, enabled):
        for btn in (self.scan_selected_btn, self.scan_all_btn):
            btn.setEnabled(enabled)
        self.close_btn.setEnabled(True)
        self.lookup_btn.setEnabled(
            enabled and self._has_scan_results() and any(r.get('ao3_url') for r in self.rows)
        )
        self.cancel_lookup_btn.setEnabled(bool(self._lookup_worker and self._lookup_worker.isRunning()))
        for spec in FIELDS:
            btn = self.apply_btns[spec['field']]
            btn.setEnabled(
                enabled and self._has_scan_results()
                and spec['field'] not in self._missing_fields
            )

    def closeEvent(self, event):
        if self._lookup_worker is not None and self._lookup_worker.isRunning():
            self._lookup_worker.cancel()
            self._lookup_worker.wait(10000)
        if self._scanning:
            self._scan_timer.stop()
            self._scanning = False
        super().closeEvent(event)

    # ---------- applying ----------

    def apply_results(self, spec):
        field = spec['field']
        key = spec['key']
        if field in self._missing_fields:
            QMessageBox.warning(
                self, 'Fanfiction Metadata Scanner',
                f'The {field} column was not found. Create it in Calibre first.'
            )
            return

        selected_ids = self._checked_book_ids()
        if not selected_ids:
            QMessageBox.information(
                self, 'Fanfiction Metadata Scanner',
                'No books are selected for updating. Check the Update? boxes.'
            )
            return

        eligible = [
            r for r in self.rows
            if r['id'] in selected_ids and r[key] is not None
            and (key in ('nsfw', 'dead_dove') or bool(r[key]))
        ]
        if not eligible:
            QMessageBox.information(
                self, 'Fanfiction Metadata Scanner',
                f'No scanned books with detected data for {field}.'
            )
            return

        answer = QMessageBox.question(
            self, 'Confirm database update',
            f'Replace the existing {field} values with detected metadata '
            f'for {len(eligible)} selected book(s)?\n\n'
            f'Tags mode: {self.tag_mode.currentText()}. '
            'Books with no detected data are left unchanged. '
            'Source files will not be modified.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        column_meta = self.db.field_metadata[field]
        is_multiple = bool(column_meta.get('is_multiple', False))
        series_indices = {}
        if key in ('nsfw', 'dead_dove'):
            mapping = {r['id']: r[key] for r in eligible}
        elif key == 'tags':
            mapping = {}
            for r in eligible:
                scraped = list(r['tags'] or [])
                if self.tag_mode.currentIndex() == 0:
                    current = self.db.field_for(
                        'tags', r['id'], default_value=()
                    ) or ()
                    if isinstance(current, str):
                        current = [current]
                    combined = list(current)
                    seen = {str(x).casefold() for x in combined}
                    for tag in scraped:
                        if tag.casefold() not in seen:
                            combined.append(tag)
                            seen.add(tag.casefold())
                    mapping[r['id']] = tuple(combined)
                else:
                    mapping[r['id']] = tuple(scraped)
        elif key == 'series':
            # Calibre books belong to exactly one series per series column:
            # use the first detection, split the part number off the name,
            # and write the numeric index to the companion index column.
            mapping = {}
            for r in eligible:
                first = r[key][0]
                m = re.match(r'^(.*?)\s+\(Part (\d+(?:\.\d+)?)\)\s*$', first)
                name, index = (m.group(1), float(m.group(2))) if m else (first, None)
                mapping[r['id']] = name
                if index is not None:
                    series_indices[r['id']] = index
        elif is_multiple:
            mapping = {r['id']: tuple(r[key]) for r in eligible}
        else:
            mapping = {r['id']: ', '.join(r[key]) for r in eligible}

        try:
            self.db.set_field(field, mapping)
            if series_indices:
                index_field = 'series_index' if field == 'series' else field + '_index'
                self.db.set_field(index_field, series_indices)
        except Exception as e:
            QMessageBox.critical(
                self, 'Update failed',
                f'Calibre could not update {field}:\n{e}'
            )
            return

        QMessageBox.information(
            self, 'Fanfiction Metadata Scanner',
            f'Updated {field} for {len(mapping)} selected book(s).'
        )
        self._refresh_existing_fields()
