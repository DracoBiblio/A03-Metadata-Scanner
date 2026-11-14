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

            # Secondary front-matter pass: AO3 metadata blocks are sometimes
            # flattened into one paragraph with adjacent inline labels.
            if opf_text:
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


def parse_safety_flags(path):
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
    explicit_rating = any(x in {'explicit', 'explicit rating'} for x in tags)
    has_explicit_tag = bool(tags.intersection(NSFW_EXPLICIT_TAGS))
    dead_dove = 'dead dove: do not eat' in tags
    return ('Yes' if explicit_rating or has_explicit_tag else 'No',
            'Yes' if dead_dove else 'No')
