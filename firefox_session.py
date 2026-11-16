"""Read AO3 cookies from the user's local Firefox profile.

Only cookies scoped to archiveofourown.org are returned. Cookie values are
kept in memory for the lookup pass; this module does not write to the profile
or persist credentials.
"""
import configparser
import os
import sqlite3


def _profiles_ini():
    appdata = os.environ.get('APPDATA')
    if not appdata:
        return None
    path = os.path.join(appdata, 'Mozilla', 'Firefox', 'profiles.ini')
    return path if os.path.isfile(path) else None


def _profile_dirs():
    ini = _profiles_ini()
    if not ini:
        return []
    cfg = configparser.RawConfigParser()
    try:
        cfg.read(ini, encoding='utf-8')
    except Exception:
        return []
    base = os.path.dirname(ini)
    candidates = []
    sections = [s for s in cfg.sections() if s.startswith('Profile')]
    # Prefer profiles explicitly marked as default, then other profiles.
    sections.sort(key=lambda s: cfg.get(s, 'Default', fallback='0') == '1', reverse=True)
    for section in sections:
        path = cfg.get(section, 'Path', fallback='').strip()
        if not path:
            continue
        if cfg.get(section, 'IsRelative', fallback='1') == '1':
            path = os.path.join(base, path)
        if os.path.isdir(path):
            candidates.append(path)
    # Also inspect profile directories if profiles.ini is incomplete.
    try:
        for name in os.listdir(os.path.join(base, 'Profiles')):
            path = os.path.join(base, 'Profiles', name)
            if os.path.isdir(path) and path not in candidates:
                candidates.append(path)
    except Exception:
        pass
    return candidates


def get_ao3_cookie_header():
    """Return (Cookie header, message); never return cookies for other sites."""
    profiles = _profile_dirs()
    if not profiles:
        return None, 'Firefox profile was not found.'
    for profile in profiles:
        db_path = os.path.join(profile, 'cookies.sqlite')
        if not os.path.isfile(db_path):
            continue
        conn = None
        try:
            # Read-only: Firefox's cookie database is never modified.
            uri = 'file:' + db_path.replace('\\', '/') + '?mode=ro'
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            rows = conn.execute(
                "SELECT name, value, host, path, expiry FROM moz_cookies "
                "WHERE host = 'archiveofourown.org' OR host = '.archiveofourown.org' "
                "OR host LIKE '%.archiveofourown.org'"
            ).fetchall()
            if rows:
                # Firefox can retain expired rows; ignore them.
                import time
                now = int(time.time())
                pairs = []
                seen = set()
                for name, value, host, path, expiry in rows:
                    if expiry and int(expiry) < now:
                        continue
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    pairs.append(str(name) + '=' + str(value))
                if pairs:
                    return '; '.join(pairs), 'Firefox AO3 cookies found.'
        except Exception:
            # Try another profile; don't expose database paths or cookie values.
            pass
        finally:
            if conn is not None:
                conn.close()
    return None, 'No active AO3 cookies were found in Firefox.'
