import html
import re
import zipfile
from html.parser import HTMLParser

AO3_WORK_RE = re.compile(r'https?://(?:www\.)?archiveofourown\.org/works/\d+', re.I)
SERIES_PART_RE = re.compile(r'^\s*Part\s+(\d+)\s+of\s+(.+?)\s*$', re.I)


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
    found = []
    pattern = re.compile(r'<dt\b[^>]*>\s*(.*?)\s*</dt>\s*<dd\b[^>]*>(.*?)</dd>', re.I | re.S)
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
        match = re.match(r'^(fandoms?|relationships?|series)\s*:\s*(.*)$', text, re.I)
        if match and match.group(2):
            found.append((match.group(1).lower(), match.group(2), 'p'))
    return found


def fanficfare_fields(raw):
    found = []
    if 'fff_titlepage' not in raw:
        return found
    for label, value in re.findall(
        r'<b\b[^>]*>\s*(Category|Relationships?|Series)\s*:\s*</b>(.*?)(?=<b\b|</(?:div|p)>|$)',
        raw, re.I | re.S
    ):
        value = visible_text(value)
        if value:
            found.append((label.lower(), value, 'fff'))
    return found


def split_values(value):
    # AO3 lists individual fandoms/relationships separated by comma-space.
    # NOTE: series values must NOT go through this — series names may contain commas.
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

# Substring guards catch warning variants ("Past Rape/Non-con", etc.)
# that exact matching misses. Checked before the relationship heuristic.
AO3_NON_FANDOM_SUBSTRINGS = (
    'archive warnings', 'rape/non-con', 'underage',
    'chose not to use', 'audiences', 'rated ',
)


def is_non_fandom_subject(value):
    v = value.casefold()
    if v in AO3_NON_FANDOM_SUBJECTS:
        return True
    return any(s in v for s in AO3_NON_FANDOM_SUBSTRINGS)


def opf_subjects(opf_text):
    values = re.findall(r'<(?:\w+:)?subject\b[^>]*>(.*?)</(?:\w+:)?subject\s*>', opf_text, re.I | re.S)
    out = []
    for x in values:
        v = clean_text(re.sub(r'<[^>]+>', '', x))
        if v:
            out.append(v)
    return out


def is_relationship_subject(value):
    if '/' in value:
        left, right = value.split('/', 1)
        if len(left.strip()) >= 4 and len(right.strip()) >= 4:
            return True
    if ' & ' in value:
        left, right = value.split(' & ', 1)
        if len(left.strip()) >= 4 and len(right.strip()) >= 4:
            return True
    return False


def fallback_subject_fields(subjects):
    # Exclusion filter runs FIRST: several AO3 warnings contain '/'
    # ("Rape/Non-con") and must not become fake relationships.
    # Subject order on AO3 OPFs: fandoms, relationships, then extra tags.
    # The saw_relationship cutoff keeps characters out of fandoms for
    # multi-ship works; gen fics (characters only) may still leak — status
    # flags this path for review.
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
    """Series fields arrive as 'Part 3 of Series Name' (AO3/FFF) or just a name."""
    match = SERIES_PART_RE.match(value)
    if match:
        part = match.group(1)
        name = clean_text(match.group(2))
        return f'{name} (Part {part})' if name else None
    return clean_text(value) or None


def parse_epub(path):
    """Returns (fandoms, ships, series, result). List values are detected
    metadata; None lists mean 'not applicable', empty lists mean none found."""
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
                r'<dc:publisher[^>]*>\s*(?:archiveofourown\.org|archive\s+of\s+our\s+own)', opf_text, re.I
            ))
            url_ao3 = bool(AO3_WORK_RE.search(opf_text))
            parsed = []
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
                parsed.extend(fanficfare_fields(raw))

            if not (publisher_ao3 or url_ao3):
                return None, None, None, 'Not identified as AO3'

            fandoms, relationships, series_list = [], [], []
            for label, value, source in parsed:
                if label in ('fandom', 'fandoms') or (label == 'category' and source == 'fff'):
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
            # Series from FanFicFare's OPF meta tags — high confidence.
            if not series_list:
                meta_name = _meta_content(opf_text, 'calibre:series')
                if meta_name:
                    meta_index = _meta_content(opf_text, 'calibre:series_index')
                    series_list.append(_format_series(meta_name, meta_index))
                    used_fallback = True
            # Labelled relationships but no labelled fandoms: consult subjects.
            if not fandoms:
                subject_fandoms, _ = fallback_subject_fields(opf_subjects(opf_text))
                if subject_fandoms:
                    fandoms.extend(subject_fandoms)
                    used_fallback = True

            if fandoms or relationships or series_list:
                if used_fallback:
                    status = 'AO3 metadata found (partly inferred — review values)'
                else:
                    status = 'AO3 metadata found'
            else:
                status = 'AO3 identified, but no fandom/relationship/series metadata found'
            return fandoms, relationships, series_list, status
    except (zipfile.BadZipFile, OSError) as e:
        return None, None, None, 'Could not read EPUB: ' + str(e)