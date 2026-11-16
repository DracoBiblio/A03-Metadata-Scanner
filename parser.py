import html
import posixpath
import re
import zipfile
from html.parser import HTMLParser

AO3_WORK_RE = re.compile(r'https?://(?:www\.)?archiveofourown\.org/works/\d+', re.I)
SERIES_PART_RE = re.compile(r'^\s*Part\s+(\d+(?:\.\d+)?)\s+of\s+(.+?)\s*$', re.I)


def clean_text(value):
    value = html.unescape(value or '')
    return re.sub(r'\s+', ' ', value).strip()


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_text(fragment):
    p = TextExtractor()
    try:
        p.feed(fragment)
    except Exception:
        pass
    return clean_text(''.join(p.parts))


def labelled_dl_fields(raw):
    pattern = re.compile(r'<dt\b[^>]*>\s*(.*?)\s*</dt>\s*<dd\b[^>]*>(.*?)</dd>', re.I | re.S)
    found = []
    for label_html, value_html in pattern.findall(raw):
        label = visible_text(label_html).rstrip(':').lower()
        value = visible_text(value_html)
        if label and value:
            found.append((label, value, 'dl'))
    return found


def labelled_paragraph_fields(raw):
    found = []
    for paragraph in re.findall(r'<p\b[^>]*>(.*?)</p>', raw, re.I | re.S):
        text = visible_text(paragraph)
        match = re.match(r'^(multi[- ]?fandoms?|fandoms?|relationships?|series)\s*:\s*(.*)$', text, re.I)
        if match and match.group(2):
            found.append((match.group(1).lower(), match.group(2), 'p'))
    return found


def fanficfare_fields(raw):
    found = []
    for label, value in re.findall(
        r'<b\b[^>]*>\s*(Category|Genre|Relationships?|Series)\s*:\s*</b>(.*?)(?=<b\b|</(?:div|p)>|$)',
        raw, re.I | re.S
    ):
        value = visible_text(value)
        if value:
            found.append((label.lower(), value, 'fff'))
    return found



AO3_INLINE_LABEL_RE = re.compile(
    r'(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))((?i:Multi[- ]?Fandoms?|Fandoms?|Relationships?|Series|Rating|Archive\s+Warning|Category|'
    r'Characters|Additional\s+Tags|Language|Stats))\s*:'
)


def first_two_spine_documents(zf, opf_text):
    """Return the first two readable content documents in EPUB spine order.

    EPUBs are usually reflowable, so these are the first two XHTML reading
    documents, not guaranteed physical reader pages.
    """
    manifest = {}
    for tag in re.findall(r'<item\b[^>]*>', opf_text, re.I | re.S):
        id_match = re.search(r'\bid\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        href_match = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        media_match = re.search(r'\bmedia-type\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if id_match and href_match and (not media_match or 'html' in media_match.group(1).lower()):
            manifest[id_match.group(1)] = href_match.group(1)

    spine_ids = re.findall(
        r'<itemref\b[^>]*\bidref\s*=\s*["\']([^"\']+)["\'][^>]*>',
        opf_text, re.I | re.S
    )
    opf_name = next((n for n in zf.namelist() if n.lower().endswith('.opf')
                     and zf.read(n).decode('utf-8', 'replace') == opf_text), None)
    base_dir = posixpath.dirname(opf_name) if opf_name else ''
    result = []
    for item_id in spine_ids:
        href = manifest.get(item_id)
        if not href:
            continue
        path = posixpath.normpath(posixpath.join(base_dir, href.split('#', 1)[0]))
        if path in zf.namelist() and path.lower().endswith(('.html', '.htm', '.xhtml')):
            try:
                result.append(zf.read(path).decode('utf-8', 'replace'))
            except Exception:
                pass
        if len(result) >= 2:
            break
    return result


def frontmatter_label_fields(zf, opf_text):
    """Extract AO3 labels embedded inline in the first two spine documents."""
    found = []
    for raw in first_two_spine_documents(zf, opf_text):
        text = visible_text(raw)
        matches = list(AO3_INLINE_LABEL_RE.finditer(text))
        for i, match in enumerate(matches):
            label = re.sub(r'\s+', ' ', match.group(1)).casefold()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            value = clean_text(text[match.end():end]).strip(' :')
            if value and label in ('fandom', 'fandoms', 'multifandom', 'multi fandom', 'multi-fandom', 'relationship',
                                   'relationships', 'series'):
                found.append((label, value, 'frontmatter'))
    return found

def split_values(value):
    # AO3 separates multi-value fields with comma-space.
    # Series values must NOT go through this (names may contain commas).
    return [clean_text(x) for x in re.split(r'\s*,\s*', value) if clean_text(x)]


AO3_NON_FANDOM_SUBJECTS = {
    'fanworks', 'no archive warnings apply', 'archive warnings',
    'graphic depictions of violence', 'major character death',
    'underage', 'rape/non-con', 'choose not to use archive warnings',
    'creator chose not to use archive warnings',
    'author chose not to use archive warnings',
    'general audiences', 'teen and up audiences', 'mature', 'explicit',
    'not rated', 'gen', 'teen', 'm/m', 'f/f', 'f/m', 'other',
}

# Substring guards catch warning variants ("Past Rape/Non-con", etc.).
AO3_NON_FANDOM_SUBSTRINGS = (
    'archive warnings', 'rape/non-con', 'underage',
    'chose not to use', 'audiences', 'rated ',
)


WORD_COUNT_RANGE_RE = re.compile(
    r'^\s*\d[\d,]*\s*[km]?(?:\s*[-–—]\s*\d[\d,]*\s*[km]?)?'
    r'(?:\s*(?:words?|word count))?\s*$',
    re.I
)


def is_non_fandom_subject(value):
    v = value.casefold()
    # AO3 word-count buckets (e.g. "20-50k") are metadata tags, not fandoms.
    if WORD_COUNT_RANGE_RE.match(value):
        return True
    if v in AO3_NON_FANDOM_SUBJECTS:
        return True
    return any(s in v for s in AO3_NON_FANDOM_SUBSTRINGS)


def opf_subjects(opf_text):
    values = re.findall(
        r'<(?:\w+:)?subject\b[^>]*>(.*?)</(?:\w+:)?subject\s*>', opf_text, re.I | re.S)
    out = []
    for x in values:
        v = clean_text(re.sub(r'<[^>]+>', '', x))
        if v:
            out.append(v)
    return out


def is_relationship_subject(value):
    # Slash is the reliable AO3 relationship separator in OPF subjects.
    # Do not treat ampersands as relationship separators here: fandom names
    # can contain '&' (e.g. 'Red White & Royal Blue - Casey McQuiston').
    # Ampersand-only platonic relationships are left to labelled metadata,
    # where the field label makes their meaning unambiguous.
    if '/' in value:
        left, right = value.split('/', 1)
        if len(left.strip()) >= 4 and len(right.strip()) >= 4:
            return True
    return False


def fallback_subject_fields(subjects):
    """Infer fandoms/relationships from OPF subjects.

    Exclusion filter runs FIRST: several AO3 warnings contain '/'
    ("Rape/Non-con") and must not become fake relationships.
    Subject order on AO3 OPFs: fandoms, relationships, extra tags.
    The saw_relationship cutoff keeps characters out of fandoms for
    multi-ship works; gen fics (characters only) may still leak —
    callers flag this path for review.
    """
    fandoms, relationships, saw_relationship = [], [], False
    for value in subjects:
        if is_non_fandom_subject(value):
            continue
        if is_relationship_subject(value):
            saw_relationship = True
            if value not in relationships:
                relationships.append(value)
        elif not saw_relationship and value not in fandoms:
            fandoms.append(value)
    return fandoms, relationships


def _meta_content(opf_text, meta_name):
    for tag in re.findall(r'<meta\b[^>]*>', opf_text, re.I):
        if re.search(r'name\s*=\s*["\']' + meta_name + r'["\']', tag, re.I):
            m = re.search(r'content\s*=\s*["\'](.*?)["\']', tag, re.I | re.S)
            if m:
                return html.unescape(m.group(1)).strip()
    return None


def _format_series(name, index=None):
    name = clean_text(name)
    if index is not None:
        try:
            n = float(index)
            part = str(int(n)) if n.is_integer() else str(n)
            return f'{name} (Part {part})'
        except ValueError:
            pass
    return name


def parse_series_value(value):
    """Series values arrive as 'Part 3 of Name' (AO3/FFF) or just a name.
    Returns 'Name (Part N)' or the bare name. Reject word-count buckets."""
    value = clean_text(value)
    if not value or WORD_COUNT_RANGE_RE.fullmatch(value):
        return None
    match = SERIES_PART_RE.match(value)
    if match:
        name = clean_text(match.group(2))
        return f'{name} (Part {match.group(1)})' if name else None
    return clean_text(value) or None


def parse_epub(path):
    """Returns (fandoms, ships, series, result).

    Lists hold detected values; None means 'not applicable'
    (e.g. not an AO3 book), empty list means none found.
    """
    try:
        with zipfile.ZipFile(path, 'r') as zf:
            names = zf.namelist()

            opf_text = ''
            for name in names:
                if name.lower().endswith('.opf'):
                    try:
                        opf_text += zf.read(name).decode('utf-8', 'replace')
                    except Exception:
                        pass

            publisher_ao3 = bool(re.search(
                r'<dc:publisher[^>]*>\s*(?:archiveofourown\.org|archive\s+of\s+our\s+own)',
                opf_text, re.I))
            url_ao3 = bool(AO3_WORK_RE.search(opf_text))

            parsed = []
            has_fff_fields = False
            for name in names:
                if not name.lower().endswith(('.html', '.htm', '.xhtml')):
                    continue
                try:
                    raw = zf.read(name).decode('utf-8', 'replace')
                except Exception:
                    continue
                if AO3_WORK_RE.search(raw):
                    url_ao3 = True
                parsed.extend(labelled_dl_fields(raw))
                parsed.extend(labelled_paragraph_fields(raw))
                fff_fields = fanficfare_fields(raw)
                if fff_fields:
                    has_fff_fields = True
                    parsed.extend(fff_fields)

            # FanFicFare bold-label fields are more precise than generic
            # paragraph/inline scans for this layout; discard overlapping
            # generic captures that can absorb an adjacent Series label.
            if has_fff_fields:
                parsed = [field for field in parsed if field[2] == 'fff']

            # Secondary front-matter pass: AO3 metadata blocks are sometimes
            # flattened into one paragraph with adjacent inline labels.
            if opf_text and not has_fff_fields:
                parsed.extend(frontmatter_label_fields(zf, opf_text))

            if not (publisher_ao3 or url_ao3):
                return None, None, None, 'Not identified as AO3'

            fandoms, relationships, series_list = [], [], []
            for label, value, source in parsed:
                if label in ('fandom', 'fandoms', 'multifandom', 'multi fandom', 'multi-fandom') or (label == 'category' and source == 'fff'):
                    for item in split_values(value):
                        if item and item not in fandoms:
                            fandoms.append(item)
                elif label in ('relationship', 'relationships'):
                    for item in split_values(value):
                        if item and item not in relationships:
                            relationships.append(item)
                elif label == 'series':
                    # No comma-splitting: series names can contain commas.
                    item = parse_series_value(value)
                    if item and item not in series_list:
                        series_list.append(item)

            used_fallback = False

            # FanFicFare writes the series into OPF meta tags — high confidence.
            if not series_list:
                meta_name = _meta_content(opf_text, 'calibre:series')
                if meta_name:
                    meta_index = _meta_content(opf_text, 'calibre:series_index')
                    series_list.append(_format_series(meta_name, meta_index))
                    used_fallback = True

            # Labelled relationships but no fandoms: consult subjects.
            if not fandoms:
                subject_fandoms, _ = fallback_subject_fields(opf_subjects(opf_text))
                if subject_fandoms:
                    fandoms.extend(subject_fandoms)
                    used_fallback = True

            if fandoms or relationships or series_list:
                status = ('AO3 metadata found (partly inferred — review values)'
                          if used_fallback else 'AO3 metadata found')
            else:
                status = 'AO3 identified, but no fandom/relationship/series metadata found'
            return fandoms, relationships, series_list, status

    except (zipfile.BadZipFile, OSError) as e:
        return None, None, None, 'Could not read EPUB: ' + str(e)


# PDF support is intentionally separate from the EPUB parser. Calibre bundles
# Poppler's pdftotext executable; locate that bundled executable without
# requiring a separately installed system copy.
def _find_pdftotext():
    import os
    import shutil
    import sys
    import calibre
    candidates = [shutil.which('pdftotext')]
    exe_dir = os.path.dirname(sys.executable)
    calibre_dir = os.path.dirname(os.path.abspath(calibre.__file__))
    candidates.extend([
        os.path.join(exe_dir, 'pdftotext.exe'),
        os.path.join(exe_dir, 'pdftotext'),
        os.path.join(exe_dir, 'bin', 'pdftotext.exe'),
        os.path.join(exe_dir, 'bin', 'pdftotext'),
        os.path.join(os.path.dirname(exe_dir), 'bin', 'pdftotext.exe'),
        os.path.join(os.path.dirname(exe_dir), 'bin', 'pdftotext'),
        os.path.join(exe_dir, 'app', 'bin', 'pdftotext.exe'),
        os.path.join(exe_dir, 'app', 'bin', 'pdftotext'),
        os.path.join(sys.prefix, 'bin', 'pdftotext.exe'),
        os.path.join(sys.prefix, 'bin', 'pdftotext'),
        os.path.join(calibre_dir, '..', '..', 'bin', 'pdftotext.exe'),
        os.path.join(calibre_dir, '..', '..', 'bin', 'pdftotext'),
    ])
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def extract_pdf_frontmatter(path):
    """Extract text from the opening PDF pages using Calibre's bundled Poppler."""
    import subprocess
    import os
    exe = _find_pdftotext()
    if not exe:
        raise RuntimeError('Calibre pdftotext utility was not found')
    # AO3 metadata normally appears on the first few pages. Limiting extraction
    # keeps scans quick even for long fanworks.
    proc = subprocess.run(
        [exe, '-f', '1', '-l', '5', '-layout', path, '-'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError('pdftotext failed: ' + proc.stderr.decode('utf-8', 'replace')[:300])
    return proc.stdout.decode('utf-8', 'replace')


def _pdf_label_values(text):
    """Read labelled AO3 fields or bullet-group metadata from PDF text.

    Labelled PDFs use comma-separated values. Some archived PDFs instead
    render each metadata item as its own bullet, so those values are kept
    with a private separator until parse_pdf splits them.
    """
    labels = {'rating', 'archive warning', 'archive warnings', 'categories', 'category', 'fandoms',
              'fandom', 'relationships', 'relationship', 'characters',
              'additional tags', 'language', 'series', 'collections', 'collection', 'stats', 'published'}

    def finish_field(key, parts):
        value = clean_text(' '.join(x for x in parts if x))
        if key == 'additional tags':
            # The PDF sometimes appends this explanatory notice to the final
            # tag, wrapping the sentence across multiple extracted lines.
            value = re.split(
                r'\bRelationship Tags update as relationships begin to evolve\b',
                value, flags=re.I)[0]
            value = clean_text(value)
        return value

    lines = [clean_text(x.replace('\x0c', ' ')) for x in text.splitlines()]
    found = []
    current = None
    parts = []
    for line in lines:
        if not line:
            continue
        m = re.match(r'^([A-Za-z][A-Za-z ]{1,30}):\s*(.*)$', line)
        label = clean_text(m.group(1)).casefold() if m else None
        if label in labels:
            if current and parts:
                value = finish_field(current, parts)
                if value:
                    found.append((current, value))
            current = label
            parts = [m.group(2)] if m.group(2) else []
        elif current:
            parts.append(line)
    if current and parts:
        value = finish_field(current, parts)
        if value:
            found.append((current, value))
    if found:
        # Some labelled and hybrid exports omit the Series label but still
        # include the standard AO3 "Part N of ..." line in the header.
        if not any(key == 'series' for key, value in found):
            series_value = _pdf_find_unlabelled_series(text)
            if series_value:
                found.append(('series', series_value))
        return found

    # Bullet-only PDF layout. A bullet starts a metadata item; non-bullet
    # lines continue the preceding item when text wraps across lines.
    groups = []
    group = []
    item = None

    def finish_item():
        nonlocal item
        if item:
            group.append(clean_text(item))
        item = None

    def finish_group():
        nonlocal group
        finish_item()
        if group:
            groups.append(group)
        group = []

    for raw in text.splitlines():
        line = clean_text(raw.replace('\x0c', ' '))
        if not line:
            finish_group()
            continue
        if line.startswith('The Wayback Machine') or line.startswith('http'):
            continue
        bullet = re.match(r'^[•●▪‣]\s*(.*)$', line)
        if bullet:
            finish_item()
            item = bullet.group(1)
        elif item:
            item += ' ' + line
        # Unbulleted page headers/navigation are deliberately not folded
        # into the previous metadata item.
    finish_group()

    if len(groups) < 7:
        return []
    first = groups[:7]
    rating_values = {'general audiences', 'teen and up audiences',
                     'mature', 'explicit', 'not rated'}
    if len(first[0]) != 1 or first[0][0].casefold() not in rating_values:
        return []
    if not any('violence' in x.casefold() or 'underage' in x.casefold()
               or 'rape' in x.casefold() or 'no archive warnings' in x.casefold()
               for x in first[1]):
        return []
    positional = ['rating', 'archive warnings', 'categories', 'fandoms',
                  'relationships', 'characters', 'additional tags']
    # Unit separator preserves bullet boundaries without confusing commas
    # that may legitimately occur inside an individual tag.
    found = [(key, '\x1f'.join(vals)) for key, vals in zip(positional, first)]
    series_value = _pdf_find_unlabelled_series(text)
    if series_value:
        found.append(('series', series_value))
    return found


def _pdf_find_unlabelled_series(text):
    """Find the AO3 Part N of Series line in bullet-layout PDF headers."""
    flat = clean_text((text or '').replace('\x0c', ' '))
    marker = re.search(
        r'Relationship Tags update as relationships begin to evolve', flat, re.I)
    if marker:
        flat = flat[marker.end():]
    else:
        flat = flat[:12000]
    match = re.search(
        r'\b(?:English\s+)?Part\s+(\d+(?:\.\d+)?)\s+of\s+(.+?)'
        r'(?=\s+(?:Next Work|Previous Work|Next Chapter|Stats:|Published:)\b|$)',
        flat, re.I)
    if not match:
        return None
    name = clean_text(match.group(2)).strip(' •●▪‣→-')
    return f'Part {match.group(1)} of {name}' if name else None


def _pdf_split_values(value):
    if '\x1f' in (value or ''):
        return [clean_text(x) for x in value.split('\x1f') if clean_text(x)]
    return split_values(value)


def parse_pdf_series_value(value):
    """Normalize AO3 PDF series labels and remove adjacent navigation text."""
    value = clean_text((value or '').replace('\x0c', ' '))
    value = re.split(
        r'\b(?:Next Work|Previous Work|Next Chapter|Previous Chapter)\b',
        value, flags=re.I)[0]
    value = re.sub(r'\s*[●•▪‣]\s*$', '', value)
    value = clean_text(value).strip(' •●▪‣→-')
    value = re.sub(r'^English\s+', '', value, flags=re.I)
    if not value:
        return None
    match = re.match(r'^(?:Part\s+)?(\d+(?:\.\d+)?)\s+of\s+(.+?)\s*$', value, re.I)
    if match:
        name = clean_text(match.group(2))
        name = re.sub(r'\s*\(Part\s+\d+(?:\.\d+)?\)\s*$', '', name, flags=re.I)
        return f'{name} (Part {match.group(1)})' if name else None
    # A few text layers omit the word "Part" and retain the ordinal only.
    roman = re.match(r'^(I|II|III|IV|V|VI|VII|VIII|IX|X)\s+of\s+(.+?)\s*$', value, re.I)
    if roman:
        numerals = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5,
                    'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10}
        return f'{clean_text(roman.group(2))} (Part {numerals[roman.group(1).upper()]})'
    return parse_series_value(value)



def detect_file_content(path):
    """Return EPUB, PDF, HTML, or UNKNOWN based on file bytes, not filename."""
    try:
        with open(path, 'rb') as f:
            head = f.read(8192)
        if head.startswith(b'%PDF-'):
            return 'PDF'
        if head.startswith(b'PK\x03\x04'):
            return 'EPUB'
        sample = head.decode('utf-8', 'replace').lstrip('\ufeff \t\r\n').lower()
        if sample.startswith('<!doctype html') or sample.startswith('<html') or '<html' in sample[:2000]:
            return 'HTML'
    except Exception:
        pass
    return 'UNKNOWN'


def find_ao3_url_in_text(text):
    """Return the first AO3 work URL in a text fragment, or None."""
    match = AO3_WORK_RE.search(text or '')
    return match.group(0) if match else None


def find_epub_ao3_url(path):
    """Find an AO3 work URL in EPUB metadata or reading content.

    Checks OPF metadata first, then the first two spine documents, then any
    remaining HTML/XHTML document as a fallback. All malformed-archive and
    individual-entry failures are treated as a missing URL.
    """
    try:
        with zipfile.ZipFile(path, 'r') as zf:
            names = zf.namelist()
            opf_texts = []
            for name in names:
                if not name.lower().endswith('.opf'):
                    continue
                try:
                    candidate = zf.read(name).decode('utf-8', 'replace')
                except Exception:
                    continue
                opf_texts.append(candidate)
                url = find_ao3_url_in_text(candidate)
                if url:
                    return url

            for opf_text in opf_texts:
                try:
                    for doc in first_two_spine_documents(zf, opf_text):
                        url = find_ao3_url_in_text(doc)
                        if url:
                            return url
                except Exception:
                    continue

            for name in names:
                if not name.lower().endswith(('.html', '.htm', '.xhtml')):
                    continue
                try:
                    url = find_ao3_url_in_text(
                        zf.read(name).decode('utf-8', 'replace'))
                except Exception:
                    continue
                if url:
                    return url
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None
    except Exception:
        return None
    return None


def find_pdf_ao3_url(path):
    """Find an AO3 work URL in extracted PDF front matter, or None."""
    try:
        return find_ao3_url_in_text(extract_pdf_frontmatter(path))
    except Exception:
        return None


def find_ao3_url(path):
    """Dispatch AO3 URL discovery by detected EPUB, PDF, or HTML content."""
    kind = detect_file_content(path)
    if kind == 'EPUB':
        return find_epub_ao3_url(path)
    if kind == 'PDF':
        return find_pdf_ao3_url(path)
    if kind == 'HTML':
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                return find_ao3_url_in_text(f.read(3 * 1024 * 1024))
        except Exception:
            return None
    return None


def _ao3_html_fields(path):
    """Extract labelled AO3 metadata from a standalone saved AO3 HTML page."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
        if not AO3_WORK_RE.search(raw) and 'archiveofourown.org' not in raw.lower():
            return None
        found = []
        pattern = re.compile(
            r'<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>',
            re.I | re.S)
        for label_html, value_html in pattern.findall(raw):
            label = visible_text(label_html).rstrip(':').strip().casefold()
            if label not in ('rating', 'archive warnings', 'categories', 'category',
                             'fandoms', 'fandom', 'relationships', 'relationship',
                             'characters', 'additional tags', 'language', 'series',
                             'stats', 'published'):
                continue
            anchors = re.findall(
                r'<a\b[^>]*class=["\'][^"\']*\btag\b[^"\']*["\'][^>]*>(.*?)</a>',
                value_html, re.I | re.S)
            if anchors:
                value = ', '.join(visible_text(x) for x in anchors if visible_text(x))
            else:
                value = visible_text(value_html)
            if label == 'additional tags':
                value = re.sub(
                    r'(?:,\s*)?Relationship Tags update as relationships begin to evolve\b',
                    '', value, flags=re.I).strip(' ,')
            elif label == 'series':
                value = re.split(
                    r'\b(?:Next Work|Previous Work|Next Chapter|Previous Chapter)\b',
                    value, flags=re.I)[0].strip(' •●▪‣→-')
            if value:
                found.append((label, value))
        return found
    except Exception:
        return None


def parse_html(path):
    """Parse a saved AO3 HTML work page using its labelled metadata definition list."""
    fields = _ao3_html_fields(path)
    if fields is None:
        return None, None, None, 'Not identified as AO3 (HTML page)'
    fandoms, ships, series_list = [], [], []
    for label, value in fields:
        if label in ('fandom', 'fandoms'):
            for item in split_values(value):
                if item and item not in fandoms:
                    fandoms.append(item)
        elif label in ('relationship', 'relationships'):
            for item in split_values(value):
                item = re.sub(r'\s*/\s*', '/', item)
                if item and item not in ships:
                    ships.append(item)
        elif label == 'series':
            item = parse_series_value(value)
            if item and item not in series_list:
                series_list.append(item)
    return fandoms, ships, series_list, 'AO3 HTML metadata found'


def parse_html_additional_tags(path):
    fields = _ao3_html_fields(path)
    if fields is None:
        return None
    for label, value in fields:
        if label == 'additional tags':
            return split_values(value)
    return []


def parse_html_safety_flags(path, additional_tags=None):
    fields = _ao3_html_fields(path) or []
    rating = {_normalise_tag(v) for k, v in fields if k == 'rating'}
    tags = set()
    for k, value in fields:
        if k == 'additional tags':
            tags.update(_normalise_tag(x) for x in split_values(value))
    if additional_tags:
        tags.update(_normalise_tag(x) for x in additional_tags)
    explicit = bool(rating.intersection({'explicit', 'explicit rating'}))
    dead_dove = 'dead dove: do not eat' in tags
    return ('Yes' if explicit or tags.intersection(NSFW_EXPLICIT_TAGS) else 'No',
            'Yes' if dead_dove else 'No')


def parse_pdf(path):
    """Return (fandoms, ships, series, result), matching parse_epub's contract."""
    try:
        text = extract_pdf_frontmatter(path)
        fields = _pdf_label_values(text)
        if not any(label == 'rating' for label, value in fields) and not re.search(
                r'https?://(?:www\.)?archiveofourown\.org/works/\d+', text, re.I):
            return None, None, None, 'Not identified as AO3 (PDF front matter not found)'
        fandoms, ships, series_list = [], [], []
        for label, value in fields:
            if label in ('fandom', 'fandoms'):
                fandoms.extend(x for x in _pdf_split_values(value) if x not in fandoms)
            elif label in ('relationship', 'relationships'):
                for x in _pdf_split_values(value):
                    x = re.sub(r'\s*/\s*', '/', x)
                    if x not in ships:
                        ships.append(x)
            elif label == 'series':
                item = parse_pdf_series_value(value)
                if item and item not in series_list:
                    series_list.append(item)
        if fandoms or ships or series_list:
            status = 'AO3 PDF metadata found'
        else:
            status = 'AO3 identified, but no fandom/relationship/series metadata found in PDF'
        return fandoms, ships, series_list, status
    except Exception as e:
        return None, None, None, 'Could not read PDF: ' + str(e)


def parse_pdf_safety_flags(path, additional_tags=None):
    try:
        fields = _pdf_label_values(extract_pdf_frontmatter(path))
        rating = { _normalise_tag(v) for k, v in fields if k == 'rating' }
        tags = set()
        for key, value in fields:
            if key == 'additional tags':
                tags.update(_normalise_tag(x) for x in _pdf_split_values(value))
        # Use the exact tag list shown in the scan preview as well. This keeps
        # safety detection in sync with the more flexible tag extractor.
        tags.update(_normalise_tag(x) for x in (additional_tags or []) if x)
        explicit = bool(rating.intersection({'explicit', 'explicit rating'}))
        dead_dove = 'dead dove: do not eat' in tags
        return ('Yes' if explicit or tags.intersection(NSFW_EXPLICIT_TAGS) else 'No',
                'Yes' if dead_dove else 'No')
    except Exception:
        return 'No', 'No'


def parse_pdf_additional_tags(path):
    try:
        fields = _pdf_label_values(extract_pdf_frontmatter(path))
        for key, value in fields:
            if key == 'additional tags':
                # Normalize layout-added spaces around slashes, which can
                # otherwise make the same AO3 tag differ between PDF exports.
                return [re.sub(r'\s*/\s*', '/', x)
                        for x in _pdf_split_values(value)]
        return []
    except Exception:
        return None


def parse_pdf_bundle(path):
    """Extract all PDF metadata from one text pass and one shared field list."""
    try:
        text = extract_pdf_frontmatter(path)
        fields = _pdf_label_values(text)
        if not any(label == 'rating' for label, value in fields) and not re.search(
                r'https?://(?:www\.)?archiveofourown\.org/works/\d+', text, re.I):
            return None, None, None, None, 'Not identified as AO3 (PDF front matter not found)'

        fandoms, ships, series_list, tags = [], [], [], []
        rating = set()
        for label, value in fields:
            if label in ('fandom', 'fandoms'):
                fandoms.extend(x for x in _pdf_split_values(value) if x not in fandoms)
            elif label in ('relationship', 'relationships'):
                for x in _pdf_split_values(value):
                    x = re.sub(r'\s*/\s*', '/', x)
                    if x and x not in ships:
                        ships.append(x)
            elif label == 'series':
                item = parse_pdf_series_value(value)
                if item and item not in series_list:
                    series_list.append(item)
            elif label == 'additional tags':
                tags.extend(re.sub(r'\s*/\s*', '/', x)
                            for x in _pdf_split_values(value) if x)
            elif label == 'rating':
                rating.add(_normalise_tag(value))

        normalized_tags = {_normalise_tag(x) for x in tags}
        explicit = bool(rating.intersection({'explicit', 'explicit rating'}))
        dead_dove = 'dead dove: do not eat' in normalized_tags
        nsfw = 'Yes' if explicit or normalized_tags.intersection(NSFW_EXPLICIT_TAGS) else 'No'
        dead = 'Yes' if dead_dove else 'No'
        status = 'AO3 PDF metadata found' if (fandoms or ships or series_list or tags) else 'AO3 identified, but no metadata fields found in PDF'
        return fandoms, ships, series_list, tags, status, nsfw, dead
    except Exception as e:
        return None, None, None, None, 'Could not read PDF: ' + str(e), 'No', 'No'


# Clear indicators of explicit sexual content. Ambiguous tags such as
# "First Time", "Awkward Sex", "Kink Negotiation", and "Bad Sex" are
# intentionally excluded unless another clear tag is present.
NSFW_EXPLICIT_TAGS = {
    'explicit sexual content', 'graphic sexual content',
    'porn', 'porn without plot', 'porn with plot', 'pwp',
    'improper use of an object', 'consensual but not safe or sane',
    'smut', 'skippable smut', 'praise kink', 'pain kink', 'bdsm', 'dom/sub',
    'free use', 'scent kink', 'alpha/beta/omega dynamics', 'a/b/o',
    'knotting', 'owner/pet', 'oral sex', 'anal sex', 'vaginal sex',
    'mutual masturbation', 'forced orgasm', 'orgasm denial',
    'dirty talk', 'public sex', 'semi-public sex', 'rape/non-con',
    'non-con', 'sex pollen', 'gentle sex', 'rough sex', 'anal fingering', 'heat/rut', 'heat', 'rut',
    'mating bond', 'somnophilia', 'cunnilingus', 'voyeurism',
    'masturbation', 'mating press', 'cuckold', 'breeding kink',
    'overstimulation', 'sexual overstimulation', 'feminization',
    'sexual overstimulation/forced orgasm', 'lube', 'sounding',
    'cockfucking', 'urethral gaping', 'come inflation', 'belly bulge',
    'large cock', 'size difference', 'tentacle sex',
    'non-human genitalia', 'rimming', 'fisting',
    # Additional clearly sexual AO3 tags reported during testing
    'threesome - m/m/m', 'plot what plot/porn without plot',
    'hand jobs', 'handjob', 'multiple orgasms', 'coming untouched',
    'dry orgasm', 'come as lube', 'creampie', 'come eating',
    'begging', 'face-fucking', 'wall sex', 'blue balls',
    'monsterfucking', 'mating cycles/in heat', 'mating run',
    'fuck or die', 'mild wife kink', 'dubious consent', 'mildly dubious consent',
    'dubcon', 'gueststarring virginity and praise kink and come eating',
    'necrophilia',
}


def _normalise_tag(value):
    value = clean_text(value).casefold()
    value = re.sub(r'[’‘]', "'", value)
    value = re.sub(r'\s+', ' ', value)
    return value.strip(' .;')


def parse_safety_flags(path, additional_tags=None):
    """Return (nsfw, dead_dove) as 'Yes'/'No' strings.

    Explicit rating is an unconditional NSFW trigger. Clear sexual tags
    trigger regardless of rating. Ambiguous tags alone do not.
    """
    subjects = []
    try:
        with zipfile.ZipFile(path, 'r') as zf:
            for name in zf.namelist():
                if name.lower().endswith('.opf'):
                    try:
                        raw = zf.read(name).decode('utf-8', 'replace')
                    except Exception:
                        continue
                    subjects.extend(opf_subjects(raw))
            # Some AO3 EPUBs keep Additional Tags only in the reading
            # document's front-matter metadata block, not in OPF subjects.
            # Read the first two XHTML spine-like documents for safety tags.
            html_names = [n for n in zf.namelist()
                          if n.lower().endswith(('.xhtml', '.html', '.htm'))]
            for name in html_names[:2]:
                try:
                    doc = zf.read(name).decode('utf-8', 'replace')
                except Exception:
                    continue
                blocks = re.findall(
                    r'Additional\s+Tags\s*:</[^>]+>\s*</p>\s*<blockquote\b[^>]*>(.*?)</blockquote>',
                    doc, re.I | re.S)
                for block in blocks:
                    for tag in re.findall(r'<a\b[^>]*>(.*?)</a>', block, re.I | re.S):
                        subjects.append(visible_text(tag))
    except (zipfile.BadZipFile, OSError):
        return 'No', 'No'

    tags = {_normalise_tag(x) for x in subjects}
    # Also classify tags extracted for the preview; front-matter and OPF
    # extraction paths are not identical across EPUB producers.
    tags.update(_normalise_tag(x) for x in (additional_tags or []) if x)
    explicit_rating = any(x in {'explicit', 'explicit rating'} for x in tags)
    has_explicit_tag = bool(tags.intersection(NSFW_EXPLICIT_TAGS))
    dead_dove = 'dead dove: do not eat' in tags
    return ('Yes' if explicit_rating or has_explicit_tag else 'No',
            'Yes' if dead_dove else 'No')


def parse_additional_tags(path):
    """Extract AO3 Additional Tags, preferring front matter and falling back to other EPUB HTML docs."""
    found = []
    try:
        with zipfile.ZipFile(path, 'r') as zf:
            opf_text = ''
            opf_name = None
            for name in zf.namelist():
                if name.lower().endswith('.opf'):
                    opf_name = name
                    opf_text = zf.read(name).decode('utf-8', 'replace')
                    break

            docs = first_two_spine_documents(zf, opf_text) if opf_text else []
            # Some EPUB converters place the AO3 metadata page later in the spine,
            # or their OPF spine is incomplete. Search all HTML documents as a
            # fallback when the first two reading documents contain no tag block.
            if not docs:
                docs = []
            all_html_names = [n for n in zf.namelist()
                              if n.lower().endswith(('.xhtml', '.html', '.htm'))]
            # FanFicFare-style AO3 exports can label Additional Tags as Genre.
            # Extract the bold-label segment directly, without absorbing the
            # adjacent Series or Relationships fields.
            for name in all_html_names:
                try:
                    raw = zf.read(name).decode('utf-8', 'replace')
                except Exception:
                    continue
                for label, value, source in fanficfare_fields(raw):
                    if label == 'genre':
                        for tag in split_values(value):
                            if tag and tag.casefold() not in {x.casefold() for x in found}:
                                found.append(tag)
            def collect_from_doc(doc):
                blocks = re.findall(
                    r'Additional\s+Tags\s*:</[^>]+>\s*</p>\s*<blockquote\b[^>]*>(.*?)</blockquote>',
                    doc, re.I | re.S)
                # AO3 EPUBs may use a semantic definition list (dt/dd).
                # Capture the dd immediately following the Additional Tags dt.
                if not blocks:
                    blocks = re.findall(
                        r'<dt\b[^>]*>\s*Additional\s+Tags\s*:\s*</dt>\s*<dd\b[^>]*>(.*?)</dd>',
                        doc, re.I | re.S)
                # AO3 EPUBs also commonly place the tag links directly after
                # "Additional Tags:" in the same paragraph, with the remaining
                # links continuing in one or more following paragraphs. In this
                # layout, collect only the front-matter segment ending at Stats:
                # so chapter/body links are never mistaken for metadata tags.
                if not blocks:
                    label = re.search(r'Additional\s+Tags\s*:', doc, re.I)
                    if label:
                        tail = doc[label.end():]
                        stats = re.search(r'<p\b[^>]*>\s*Stats\s*:', tail, re.I | re.S)
                        if stats:
                            blocks.append(tail[:stats.start()])
                        else:
                            # Fallback for formats without a Stats paragraph:
                            # stay within the containing paragraph/blockquote.
                            p_end = re.search(r'</(?:p|blockquote)>', tail, re.I)
                            if p_end:
                                blocks.append(tail[:p_end.start()])
                for block in blocks:
                    for tag_html in re.findall(r'<a\b[^>]*>(.*?)</a>', block, re.I | re.S):
                        tag = visible_text(tag_html)
                        if tag:
                            # AO3 exports sometimes split a single tag over
                            # adjacent links; normalize whitespace and dedupe.
                            tag = re.sub(r'\s+', ' ', tag).strip(' ,\t\r\n')
                            if tag and tag.casefold() not in {x.casefold() for x in found}:
                                found.append(tag)

            for doc in docs:
                collect_from_doc(doc)
            if not found:
                for name in all_html_names:
                    try:
                        collect_from_doc(zf.read(name).decode('utf-8', 'replace'))
                    except Exception:
                        continue

            # Some FanFicFare EPUBs omit the HTML metadata page entirely but
            # retain AO3 Additional Tags as leading dc:subject entries in the
            # OPF. In these files the fandom entries provide a useful boundary:
            # collect the subject entries before the first recognizable fandom,
            # excluding obvious structural metadata. Do not fall back to every
            # subject, which would pollute Calibre Tags with fandoms/ships/etc.
            if not found and opf_text:
                subjects = opf_subjects(opf_text)
                fandom_boundary = None
                for i, subject in enumerate(subjects):
                    # AO3/FanFicFare fandom subjects commonly include a
                    # canonical title/author separator or bilingual title pair.
                    if ' | ' in subject and not is_relationship_subject(subject):
                        fandom_boundary = i
                        break
                if fandom_boundary is not None:
                    structural = {
                        'gen', 'f/f', 'f/m', 'm/m', 'multi', 'other', 'no category',
                        'complete', 'completed', 'incomplete', 'work in progress',
                        'fanfiction', 'fanworks', 'not rated', 'general audiences',
                        'teen and up audiences', 'mature', 'explicit',
                        'no archive warnings apply', 'choose not to use archive warnings',
                    }
                    for subject in subjects[:fandom_boundary]:
                        value = clean_text(subject)
                        norm = _normalise_tag(value)
                        if not value or norm in structural or is_non_fandom_subject(value):
                            continue
                        if is_relationship_subject(value):
                            continue
                        if norm not in { _normalise_tag(x) for x in found }:
                            found.append(value)
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return found
