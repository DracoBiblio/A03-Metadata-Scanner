# worker.py
#
# Worker thread wrapper around the throttled online-lookup batch helper.
# fetch_ao3_metadata_batch() blocks (network timeouts plus mandatory delays),
# so it must never run on the GUI thread. This worker emits per-book results
# via signals, which Qt delivers on the GUI thread automatically.

from qt.core import QThread, pyqtSignal

from calibre_plugins.ao3_fandom_scanner.online_lookup import fetch_ao3_metadata_batch


class LookupWorker(QThread):
    """Run online AO3 lookups off the GUI thread.

    Signals:
        book_done(book_id, result) — one result dict per book, streamed as
            each fetch completes so the preview can update incrementally.
        finished_all(results) — emitted once with {book_id: result} when the
            batch ends (including early cancellation).

    Usage: keep a reference to the worker for as long as it runs (a dangling
    worker can be garbage-collected mid-run), connect the signals, call
    start(), and call cancel() if the user aborts.
    """

    book_done = pyqtSignal(object, dict)
    finished_all = pyqtSignal(dict)

    def __init__(self, items, delay=2.0, cookie_header=None, parent=None):
        super().__init__(parent)
        self._items = list(items)
        self._delay = delay
        # Keep optional AO3 session cookies in memory for this lookup pass.
        self._cookie_header = cookie_header
        self._cancelled = False

    def cancel(self):
        """Stop after the current fetch; already-completed results are kept."""
        self._cancelled = True

    def run(self):
        results = fetch_ao3_metadata_batch(
            self._items,
            delay=self._delay,
            progress=self._emit_progress,
            should_continue=lambda: not self._cancelled,
            cookie_header=self._cookie_header,
        )
        self.finished_all.emit(results)

    def _emit_progress(self, key, result):
        self.book_done.emit(key, result)
