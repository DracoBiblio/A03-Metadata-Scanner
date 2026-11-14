from qt.core import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                     QTableWidget, QTableWidgetItem, QMessageBox, QHeaderView, QGroupBox,
                     Qt, QAbstractItemView, QTimer, QComboBox, QLineEdit)
from functools import partial
from calibre_plugins.ao3_fandom_scanner.parser import parse_epub, parse_safety_flags

# Central column configuration — make this read from plugin prefs later.
FIELDS = [
    {'field': '#fandom', 'key': 'fandoms'},
    {'field': '#ship',   'key': 'ships'},
    {'field': 'series', 'key': 'series'},
    {'field': '#nsfw', 'key': 'nsfw'},
    {'field': '#dead_dove', 'key': 'dead_dove'},
]
# Small batches per timer tick keep the UI responsive while scanning.
SCAN_BATCH_SIZE = 2

COLUMN_LABELS = [
    'Update?', 'Book', 'Fandom found', 'Fandom now',
    'Relationships found', 'Ship now',
    'Series found', 'Series now',
    'NSFW found', 'NSFW now',
    'Dead Dove found', 'Dead Dove now', 'Scan result',
]


class ScannerDialog(QDialog):
    def __init__(self, gui):
        super().__init__(gui)
        self.gui = gui
        self.db = gui.current_db.new_api
        self.rows = []
        self.pending_ids = []
        self._scanning = False
        self.setWindowTitle('AO3 Metadata Scanner — Preview')
        self.resize(1420, 760)
        self.setMinimumSize(1050, 600)

        # Validate target columns once, up front; disable the apply
        # button for anything that doesn't exist yet.
        self._missing_fields = {
            spec['field'] for spec in FIELDS
            if spec['field'] not in self.db.field_metadata
        }

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Review AO3 metadata found in your EPUBs. '
            'Preview does not change book data. Existing custom-column values '
            'are shown. Applying replaces existing values for fields with detected data.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Keep scanning actions separate from metadata replacement actions.
        scan_buttons = QHBoxLayout()
        self.scan_selected_btn = QPushButton('Scan selected books')
        self.scan_all_btn = QPushButton('Scan all EPUBs')
        self.apply_btns = {}
        for spec in FIELDS:
            btn = QPushButton(f'Replace {spec["field"].lstrip("#").replace("_", " ").title()}')
            btn.setEnabled(False)
            if spec['field'] in self._missing_fields:
                btn.setEnabled(False)
                btn.setToolTip(
                    f'The {spec["field"]} column does not exist. '
                    'Create it in Calibre preferences first.'
                )
            self.apply_btns[spec['field']] = btn

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
        for spec in FIELDS:
            apply_layout.addWidget(self.apply_btns[spec['field']])
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
        self.search_box.setPlaceholderText('Type to find a book, fandom, ship, or tag…')
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
        # Give long-text columns room while keeping compact Yes/No columns narrow.
        widths = [72, 230, 210, 190, 230, 210, 180, 170, 105, 105, 125, 125, 240]
        for c, width in enumerate(widths):
            self.table.setColumnWidth(c, width)
        layout.addWidget(self.table)

        self.status = QLabel('Choose selected books or scan all EPUBs.')
        layout.addWidget(self.status)

        self.scan_selected_btn.clicked.connect(self.scan_selected)
        self.scan_all_btn.clicked.connect(self.scan_all)
        for spec in FIELDS:
            self.apply_btns[spec['field']].clicked.connect(partial(self.apply_results, spec))
        self.close_btn.clicked.connect(self.close)
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
            QMessageBox.information(self, 'AO3 Metadata Scanner',
                                    'Select one or more books in the library first.')
            return
        self.scan_ids(ids)

    def scan_all(self):
        try:
            ids = list(self.db.search('format:EPUB'))
        except Exception:
            ids = [bid for bid in self.db.all_book_ids() if 'EPUB' in self.db.formats(bid)]
        self.scan_ids(sorted(ids))

    def scan_ids(self, ids):
        self.rows = []
        self.table.setRowCount(0)
        self.pending_ids = [bid for bid in ids if 'EPUB' in self.db.formats(bid)]
        self._scanning = True
        self._set_buttons_enabled(False)
        self.status.setText(f'Scanning {len(self.pending_ids)} EPUB(s)…')
        self._scan_timer.start(0)

    def _scan_batch(self):
        for bid in self.pending_ids[:SCAN_BATCH_SIZE]:
            self._scan_one(bid)
        del self.pending_ids[:SCAN_BATCH_SIZE]
        if self.pending_ids:
            self.status.setText(f'Scanning… {len(self.rows)} done, {len(self.pending_ids)} remaining')
        else:
            self._scan_timer.stop()
            self._scanning = False
            self._set_buttons_enabled(True)
            self.apply_table_filter()

    def _scan_one(self, bid):
        mi = self.db.get_metadata(bid)
        row = {'id': bid, 'title': mi.title or '(untitled)', 'result': ''}
        for spec in FIELDS:
            row[spec['key']] = None
            row['existing_' + spec['key']] = self._field_text(spec['field'], bid)
        path = self.db.format_abspath(bid, 'EPUB')
        if not path:
            row['nsfw'], row['dead_dove'] = 'No', 'No'
            row['result'] = 'EPUB file unavailable'
        else:
            try:
                fandoms, ships, series, result = parse_epub(path)
            except Exception as e:
                fandoms = ships = series = None
                result = f'Parse error: {e}'
            row['fandoms'], row['ships'], row['series'] = fandoms, ships, series
            row['nsfw'], row['dead_dove'] = parse_safety_flags(path)
            row['result'] = result
        self.rows.append(row)
        self._append_row_to_table(row)

    # ---------- table ----------

    def _field_text(self, field, bid):
        try:
            value = self.db.field_for(field, bid, default_value=())
        except Exception:
            return ''  # column missing — guarded by button state elsewhere
        if isinstance(value, str):
            return value
        return ', '.join(value or ())

    def _row_display_values(self, row):
        return (
            '',
            row['title'],
            ', '.join(row['fandoms']) if row['fandoms'] else '',
            row['existing_fandoms'],
            ', '.join(row['ships']) if row['ships'] else '',
            row['existing_ships'],
            ', '.join(row['series']) if row['series'] else '',
            row['existing_series'],
            row['nsfw'] or '', row['existing_nsfw'],
            row['dead_dove'] or '', row['existing_dead_dove'],
            row['result'],
        )

    def _append_row_to_table(self, row):
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, value in enumerate(self._row_display_values(row)):
            item = QTableWidgetItem(value)
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
                searchable = ' '.join(str(x) for x in self._row_display_values(row)).casefold()
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
        # Re-read column values from the DB (no re-parsing of EPUB files).
        for row in self.rows:
            for spec in FIELDS:
                row['existing_' + spec['key']] = self._field_text(spec['field'], row['id'])
        self.table.setRowCount(0)
        for row in self.rows:
            self._append_row_to_table(row)
        self.apply_table_filter()

    def _update_status(self):
        counts = [sum(1 for x in self.rows if x[s['key']]) for s in FIELDS]
        labels = ['#fandom', '#ship', '#series', '#nsfw', '#dead_dove']
        summary = '; '.join(f'{n} with {lbl} data' for n, lbl in zip(counts, labels))
        selected = len(self._checked_book_ids())
        visible = sum(1 for r in range(self.table.rowCount()) if not self.table.isRowHidden(r))
        self.status.setText(
            f'Checked {len(self.rows)} EPUB record(s); {visible} visible; '
            f'{selected} selected for update; {summary}.'
        )

    def _has_scan_results(self):
        return bool(self.rows)

    def _set_buttons_enabled(self, enabled):
        for btn in (self.scan_selected_btn, self.scan_all_btn):
            btn.setEnabled(enabled)
        # Close stays usable so the user can always leave the dialog;
        # closing mid-scan stops the timer first (see closeEvent).
        self.close_btn.setEnabled(True)
        for spec in FIELDS:
            btn = self.apply_btns[spec['field']]
            if spec['field'] in self._missing_fields:
                btn.setEnabled(False)
            else:
                btn.setEnabled(enabled and self._has_scan_results())

    def closeEvent(self, event):
        if self._scanning:
            self._scan_timer.stop()
            self._scanning = False
        super().closeEvent(event)

    # ---------- applying ----------

    def apply_results(self, spec):
        field = spec['field']
        key = spec['key']
        existing_key = 'existing_' + key
        if field in self._missing_fields:
            QMessageBox.warning(self, 'AO3 Metadata Scanner',
                                f'The {field} column was not found. Create it in Calibre first.')
            return
        # Replace existing values, but only when the EPUB supplied a usable result.
        # Empty fandom/ship/series detections are not written over good metadata;
        # safety flags are always explicit Yes/No results.
        selected_ids = self._checked_book_ids()
        if not selected_ids:
            QMessageBox.information(
                self, 'AO3 Metadata Scanner',
                'No books are selected for updating. Check the Update? boxes beside the books you want.'
            )
            return
        eligible = [
            r for r in self.rows
            if (r['id'] in selected_ids and r[key] is not None
                and (key in ('nsfw', 'dead_dove') or bool(r[key])))
        ]
        if not eligible:
            QMessageBox.information(self, 'AO3 Metadata Scanner',
                                    f'No scanned books with detected data for {field}.')
            return
        answer = QMessageBox.question(
            self, 'Confirm database update',
            f'Replace the existing {field} values with detected EPUB metadata for {len(eligible)} selected book(s)?\n\n'
            f'This overwrites current Calibre values for these books. Books with no detected '
            f'fandom/ship/series value are left unchanged. EPUB files will not be modified.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        column_meta = self.db.field_metadata[field]
        is_multiple = bool(column_meta.get('is_multiple', False))
        if key in ('nsfw', 'dead_dove'):
            mapping = {r['id']: r[key] for r in eligible}
        elif is_multiple:
            mapping = {r['id']: tuple(r[key]) for r in eligible}
        else:
            mapping = {r['id']: ', '.join(r[key]) for r in eligible}
        try:
            self.db.set_field(field, mapping)
        except Exception as e:
            QMessageBox.critical(self, 'Update failed', f'Calibre could not update {field}:\n{e}')
            return
        QMessageBox.information(self, 'AO3 Metadata Scanner',
                                f'Updated {field} for {len(mapping)} selected book(s).')
        self._refresh_existing_fields()
