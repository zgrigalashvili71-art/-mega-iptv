#!/usr/bin/env python3
"""
build_all_working_v3.py
=======================

Consolidates every IPTV health-check result folder (V1 / V2 / V3 checker
output) into ONE deduplicated, ranked, AquaPlay-ready master playlist.

It only READS the result folders. It never edits or deletes them, and it
never touches ALL_TV_PLUS_SPORTS.m3u.

Supported result schemas (detected automatically per folder):
  V3  results.csv/json with  status=WORKING_NOW|NOT_WORKING_NOW, successes, ...
  V2  results.csv/json with  final_status, confidence, segment_ok, ...
      (+ optional health_history.json)
  V1  results.csv/json with  status=HEALTHY|FLAKY|RESTRICTED|DEAD|..., has_video
  M3U-only folders (HEALTHY.m3u / WORKING_NOW.m3u ... without results files)

Metadata (tvg-id, tvg-logo, group-title, #EXTVLCOPT headers) is taken from
the M3U files that sit next to the results, matched by stream URL.

Outputs (in --output):
  ALL_WORKING_TV_V3.m3u                  master playlist (primary streams only)
  ALL_WORKING_TV_V3_REPORT.txt           statistics
  ALL_WORKING_TV_V3_SOURCES.csv / .json  provenance per final channel
  ALL_WORKING_TV_V3_BACKUPS.json         ranked backup URLs per channel
  ALL_WORKING_TV_V3_EXCLUDED.csv         everything not used, with reason
  ALL_WORKING_TV_V3_RECHECK_RESTRICTED.m3u   403/401/451 streams to re-test
  ALL_WORKING_TV_V3_RECHECK_OFFLINE_NOW.m3u  temporarily failing streams
  ALL_WORKING_TV_V3_SMOKE.csv            optional final ffprobe smoke test

Examples:
  py -3.11 build_all_working_v3.py --input "C:\\Users\\me\\Downloads\\IPTV_TEST" --output .
  python build_all_working_v3.py --input health_data --output .          (GitHub Actions)
  python build_all_working_v3.py --input health_data --output . --smoke 60
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

OUT_BASE = "ALL_WORKING_TV_V3"
PROTECTED_FILES = {"ALL_TV_PLUS_SPORTS.m3u"}
MAX_BACKUPS = 1000      # effectively unlimited: every working copy is kept
MIN_COUNTRY_GROUP = 8   # smaller "International | <Country>" groups fold into "International"
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# EPG guides (AquaPlay reads x-tvg-url), in priority order. Overridden by
# config/epg_sources.txt. With --epg the builder downloads them, matches channels
# and lists only the guides that matched something.
EPG_URLS = [
    "https://www.open-epg.com/files/georgia1.xml.gz",
    "https://i.mjh.nz/PlutoTV/all.xml.gz",
    "https://i.mjh.nz/SamsungTVPlus/all.xml.gz",
    "https://i.mjh.nz/Plex/all.xml.gz",
]

# Extra playlists published next to the full one: file suffix -> groups.
SUBSET_PLAYLISTS = {
    "GEORGIA": ["Georgia", "Georgia Radio"],
    "SPORTS": ["Sports", "Football", "Basketball", "Motorsport", "Combat Sports", "Other Sports"],
    "GEORGIA_SPORTS": ["Georgia", "Georgia Radio", "Sports", "Football", "Basketball",
                       "Motorsport", "Combat Sports", "Other Sports"],
}

# Hosts never published. Built from the 2026-10-21 GitHub piracy reporting
# pack (premium restreams / pay-TV relays). Extend via --denylist file.
BUILTIN_DENYLIST = [
    "158.101.222.193",      # PHP proxy for Setanta / Silk Sport Georgia
    "moveonjoy.com",        # US cable restreams
    "thetvapp.to", "tvpass.org", "thetvapp",
    "daddylive", "dlhd", "daddylivehd", "dlhd.so", "dlhd.dad",
    "crichd", "platinsport", "streamed.su", "streamed.pk",
    "mcquack.net",          # Russian pay-TV relays
    "televizia.org", "mar.tv",
    "fancode", "sonyliv", "jiotv", "jio.com", "toffeelive", "aynaott",
    "tapmad", "akashgo", "dishtv", "d2h.com", "yupptv",
]

# ---------------------------------------------------------------------------
# Countries
# ---------------------------------------------------------------------------
_COUNTRIES = (
    "AD Andorra|AE UAE|AF Afghanistan|AL Albania|AM Armenia|AO Angola|AR Argentina|"
    "AT Austria|AU Australia|AW Aruba|AZ Azerbaijan|BA Bosnia|BB Barbados|BD Bangladesh|"
    "BE Belgium|BF Burkina Faso|BG Bulgaria|BH Bahrain|BJ Benin|BO Bolivia|BR Brazil|"
    "BS Bahamas|BY Belarus|BZ Belize|CA Canada|CD DR Congo|CG Congo|CH Switzerland|"
    "CI Ivory Coast|CL Chile|CM Cameroon|CN China|CO Colombia|CR Costa Rica|CU Cuba|"
    "CV Cape Verde|CW Curacao|CY Cyprus|CZ Czechia|DE Germany|DK Denmark|DO Dominican Republic|"
    "DZ Algeria|EC Ecuador|EE Estonia|EG Egypt|ES Spain|ET Ethiopia|FI Finland|FJ Fiji|"
    "FO Faroe Islands|FR France|GA Gabon|GB UK|GE Georgia|GH Ghana|GN Guinea|GP Guadeloupe|"
    "GQ Equatorial Guinea|GR Greece|GT Guatemala|GU Guam|GY Guyana|HK Hong Kong|HN Honduras|"
    "HR Croatia|HT Haiti|HU Hungary|ID Indonesia|IE Ireland|IL Israel|IN India|IQ Iraq|"
    "IR Iran|IS Iceland|IT Italy|JM Jamaica|JO Jordan|JP Japan|KE Kenya|KG Kyrgyzstan|"
    "KH Cambodia|KR South Korea|KW Kuwait|KZ Kazakhstan|LA Laos|LB Lebanon|LC Saint Lucia|"
    "LI Liechtenstein|LK Sri Lanka|LT Lithuania|LU Luxembourg|LV Latvia|LY Libya|MA Morocco|"
    "MC Monaco|MD Moldova|ME Montenegro|MG Madagascar|MK North Macedonia|ML Mali|MM Myanmar|"
    "MN Mongolia|MO Macau|MQ Martinique|MR Mauritania|MT Malta|MU Mauritius|MV Maldives|"
    "MW Malawi|MX Mexico|MY Malaysia|MZ Mozambique|NA Namibia|NE Niger|NG Nigeria|"
    "NI Nicaragua|NL Netherlands|NO Norway|NP Nepal|NZ New Zealand|OM Oman|PA Panama|PE Peru|"
    "PF French Polynesia|PG Papua New Guinea|PH Philippines|PK Pakistan|PL Poland|"
    "PR Puerto Rico|PS Palestine|PT Portugal|PY Paraguay|QA Qatar|RE Reunion|RO Romania|"
    "RS Serbia|RU Russia|RW Rwanda|SA Saudi Arabia|SD Sudan|SE Sweden|SG Singapore|"
    "SI Slovenia|SK Slovakia|SL Sierra Leone|SM San Marino|SN Senegal|SO Somalia|SR Suriname|"
    "SV El Salvador|SY Syria|TD Chad|TG Togo|TH Thailand|TJ Tajikistan|TM Turkmenistan|"
    "TN Tunisia|TR Turkey|TT Trinidad and Tobago|TW Taiwan|TZ Tanzania|UA Ukraine|UG Uganda|"
    "US USA|UY Uruguay|UZ Uzbekistan|VA Vatican|VE Venezuela|VI US Virgin Islands|VN Vietnam|"
    "XK Kosovo|YE Yemen|ZA South Africa|ZM Zambia|ZW Zimbabwe"
)
COUNTRY = {c[:2]: c[3:] for c in _COUNTRIES.split("|")}
CODE_ALIASES = {
    "UK": "GB", "USA": "US", "GEO": "GE", "UKR": "UA", "RUS": "RU", "TUR": "TR",
    "ARG": "AR", "BRA": "BR", "MEX": "MX", "ESP": "ES", "FRA": "FR", "GER": "DE",
    "DEU": "DE", "ITA": "IT", "POR": "PT", "POL": "PL", "CAN": "CA", "AUS": "AU",
    "IND": "IN", "NLD": "NL", "BEL": "BE", "SWE": "SE", "NOR": "NO", "DNK": "DK",
    "FIN": "FI", "GRC": "GR", "ROU": "RO", "BGR": "BG", "SRB": "RS", "HRV": "HR",
    "ARM": "AM", "AZE": "AZ", "KAZ": "KZ", "CHL": "CL", "COL": "CO", "PER": "PE",
    "VEN": "VE", "ECU": "EC", "URY": "UY", "KSA": "SA", "QAT": "QA", "IRN": "IR",
    "ISR": "IL", "EGY": "EG", "MAR": "MA", "DZA": "DZ", "TUN": "TN", "PAK": "PK",
    "BGD": "BD", "IDN": "ID", "MYS": "MY", "PHL": "PH", "THA": "TH", "VNM": "VN",
    "JPN": "JP", "KOR": "KR", "CHN": "CN", "ZAF": "ZA", "NGA": "NG", "KEN": "KE",
}
# Two-letter prefixes that look like ISO codes but are usually not countries.
NOT_COUNTRY_PREFIX = {"TV", "HD", "FM", "AM", "SD", "VIP", "PPV", "TNT", "AXN", "BBC",
                      "CNN", "HBO", "FOX", "ABC", "NBC", "CBS", "MTV", "ESPN", "BT", "MY",
                      "IN", "IS", "IT", "NO", "ME", "TO", "GO", "AS", "AT", "BE", "SO"}
COUNTRY_NAME_TO_CODE = {}
for _code, _name in COUNTRY.items():
    COUNTRY_NAME_TO_CODE[_name.casefold()] = _code
COUNTRY_NAME_TO_CODE.update({
    "united states": "US", "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "españa": "ES", "espana": "ES", "deutschland": "DE", "türkiye": "TR", "turkiye": "TR",
    "brasil": "BR", "méxico": "MX", "italia": "IT", "россия": "RU", "україна": "UA",
    "sakartvelo": "GE", "საქართველო": "GE", "грузия": "GE", "south korea": "KR",
    "korea": "KR", "czech republic": "CZ", "united arab emirates": "AE", "uae": "AE",
    "bosnia and herzegovina": "BA", "the netherlands": "NL", "holland": "NL",
    # Spanish / Portuguese spellings used by Latin-American lists
    "estados unidos": "US", "eeuu": "US", "ee.uu.": "US", "reino unido": "GB",
    "alemania": "DE", "francia": "FR", "rusia": "RU", "turquia": "TR", "turquía": "TR",
    "kenia": "KE", "barein": "BH", "baréin": "BH", "mexico": "MX", "perú": "PE",
    "panamá": "PA", "canadá": "CA", "japon": "JP", "japón": "JP", "suiza": "CH",
    "suecia": "SE", "noruega": "NO", "dinamarca": "DK", "holanda": "NL", "paises bajos": "NL",
    "países bajos": "NL", "belgica": "BE", "bélgica": "BE", "grecia": "GR", "polonia": "PL",
    "rumania": "RO", "ucrania": "UA", "marruecos": "MA", "egipto": "EG", "arabia saudita": "SA",
    "emiratos arabes unidos": "AE", "catar": "QA", "irak": "IQ", "iran": "IR", "irán": "IR",
    "libano": "LB", "líbano": "LB", "siria": "SY", "jordania": "JO", "india": "IN",
    "corea del sur": "KR", "republica dominicana": "DO", "república dominicana": "DO",
    "haití": "HT", "brazil": "BR", "hungria": "HU", "hungría": "HU", "croacia": "HR",
    "eslovenia": "SI", "eslovaquia": "SK", "chequia": "CZ", "republica checa": "CZ",
    "finlandia": "FI", "irlanda": "IE", "escocia": "GB", "gales": "GB", "inglaterra": "GB",
    "sudafrica": "ZA", "sudáfrica": "ZA", "nigeria": "NG", "argelia": "DZ", "tunez": "TN",
    "túnez": "TN", "filipinas": "PH", "tailandia": "TH", "indonesia": "ID", "malasia": "MY",
    "australia": "AU", "nueva zelanda": "NZ", "portugal": "PT", "chipre": "CY",
    "latinoamerica": "", "latinoamérica": "",
})
COUNTRY_NAME_TO_CODE = {k: v for k, v in COUNTRY_NAME_TO_CODE.items() if v}

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def clean_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def to_int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def to_float(v, default=0.0) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


ATTR_RE = re.compile(r'([A-Za-z0-9_\-:]+)\s*=\s*"([^"]*)"')


def split_extinf(line: str) -> tuple[dict, str]:
    """Return (attributes, display name) for an #EXTINF line.

    The name starts after the first comma that is OUTSIDE quotes, so commas
    inside attribute values (e.g. 'KHTML, like Gecko') do not break it.
    """
    body = line.split(":", 1)[1] if ":" in line else line
    inq = False
    cut = -1
    for i, ch in enumerate(body):
        if ch == '"':
            inq = not inq
        elif ch == "," and not inq:
            cut = i
            break
    if cut == -1:  # unbalanced quotes: fall back to the last comma
        cut = body.rfind(",")
    head = body[:cut] if cut >= 0 else body
    name = body[cut + 1:] if cut >= 0 else ""
    attrs = {k.lower(): v.strip() for k, v in ATTR_RE.findall(head)}
    return attrs, clean_ws(name)


@dataclass
class M3UEntry:
    url: str
    name: str
    attrs: dict
    user_agent: str = ""
    referer: str = ""
    origin: str = ""
    cookie: bool = False
    drm_props: bool = False


def parse_m3u_file(path: Path) -> list[M3UEntry]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return []
    out: list[M3UEntry] = []
    attrs: dict = {}
    name = ""
    ua = ref = origin = ""
    cookie = drm = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        if up.startswith("#EXTINF"):
            attrs, name = split_extinf(line)
            ua = attrs.get("http-user-agent") or attrs.get("user-agent") or ""
            ref = (attrs.get("http-referrer") or attrs.get("http-referer")
                   or attrs.get("referrer") or attrs.get("referer") or "")
            origin = attrs.get("origin") or attrs.get("http-origin") or ""
            cookie = drm = False
            continue
        if up.startswith("#EXTVLCOPT:"):
            opt = line.split(":", 1)[1]
            k, _, v = opt.partition("=")
            k = k.strip().lower()
            if k == "http-user-agent":
                ua = v.strip()
            elif k in ("http-referrer", "http-referer"):
                ref = v.strip()
            elif k == "http-origin":
                origin = v.strip()
            elif "cookie" in k:
                cookie = True
            continue
        if up.startswith("#KODIPROP"):
            if "license" in line.lower() or "drm" in line.lower():
                drm = True
            continue
        if up.startswith("#EXTHTTP:"):
            try:
                obj = json.loads(line.split(":", 1)[1])
                ua = obj.get("User-Agent") or obj.get("user-agent") or ua
                ref = obj.get("Referer") or obj.get("Referrer") or ref
                origin = obj.get("Origin") or origin
                if any("cookie" in k.lower() or "authorization" in k.lower() for k in obj):
                    cookie = True
            except Exception:
                pass
            continue
        if line.startswith("#"):
            continue
        out.append(M3UEntry(line, name, attrs, clean_ws(ua), clean_ws(ref),
                            clean_ws(origin), cookie, drm))
        attrs, name, ua, ref, origin, cookie, drm = {}, "", "", "", "", False, False
    return out


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------
@dataclass
class Obs:
    folder: str
    schema: str
    raw_status: str
    name: str
    group: str
    url: str
    attempts: int = 0
    successes: int = 0
    media: bool = False          # ffprobe saw audio/video at least once
    played: bool = False         # ffmpeg decoded a sample at least once
    has_video: bool = False
    has_audio: bool = False
    segment_ok: bool = False
    startup: float = 0.0
    http_code: str = ""
    reason: str = ""
    hist_success: int = 0
    hist_fail: int = 0
    meta: M3UEntry | None = None
    obs_class: str = ""          # HEALTHY / FLAKY_EVIDENCE / FLAKY_NO_EVIDENCE / failure


def read_rows(folder: Path) -> list[dict]:
    csv_p = folder / "results.csv"
    json_p = folder / "results.json"
    if csv_p.exists():
        try:
            with csv_p.open(encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        except Exception as ex:
            print(f"  ! {csv_p}: {ex}; trying JSON")
    if json_p.exists():
        try:
            data = json.loads(json_p.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                data = data.get("results") or list(data.values())
            return [r for r in data if isinstance(r, dict)]
        except Exception as ex:
            print(f"  ! {json_p}: {ex}")
    return []


def detect_schema(rows: list[dict]) -> str:
    if not rows:
        return "M3U_ONLY"
    keys = set(rows[0].keys())
    if "successes" in keys and "required_successes" in keys:
        return "V3"
    if "final_status" in keys:
        return "V2"
    if "status" in keys:
        return "V1"
    return "UNKNOWN"


STATUS_FILES = {
    "WORKING_NOW", "NOT_WORKING_NOW", "HEALTHY", "FLAKY", "RESTRICTED",
    "OFFLINE_NOW", "DRM_OR_UNSUPPORTED", "DEAD", "UNKNOWN",
}

DRM_WORDS = ("widevine", "playready", "clearkey", "contentprotection", "drm")
DNS_WORDS = ("name resolution", "getaddrinfo", "name or service not known",
             "no such host", "failed to resolve", "nodename nor servname")
TEMP_WORDS = ("timeout", "timed out", "-138", "10061", "refused", "reset",
              "429", "5xx", "server error", "temporarily", "unreachable",
              "end of file", "i/o error", "connection")


def failure_class(http_code: str, reason: str, raw_status: str = "") -> str:
    low = (reason or "").lower()
    rs = (raw_status or "").upper()
    if rs == "DRM_OR_UNSUPPORTED" or any(w in low for w in DRM_WORDS):
        return "DRM"
    if rs == "RESTRICTED":
        return "RESTRICTED"
    if rs == "OFFLINE_NOW":
        return "OFFLINE_NOW"
    if rs == "DEAD":
        return "DEAD"
    code = (http_code or "").strip()
    if code in {"401", "403", "451", "407"} or "access restriction" in low:
        return "RESTRICTED"
    if code in {"404", "410"} or "hard failure" in low:
        return "DEAD"
    if any(w in low for w in DNS_WORDS):
        return "DEAD"
    if code.startswith("5") or code in {"429", "408", "200", "206", "204", "302"}:
        return "OFFLINE_NOW"
    if any(w in low for w in TEMP_WORDS):
        return "OFFLINE_NOW"
    return "UNKNOWN"


def classify_obs(o: Obs) -> str:
    rs = o.raw_status.upper()
    if o.schema == "V3":
        if rs == "WORKING_NOW":
            return "HEALTHY"
        if o.successes >= 1:
            return "FLAKY_EVIDENCE"
        return failure_class(o.http_code, o.reason)
    if o.schema in ("V1", "V2"):
        if rs == "HEALTHY":
            return "HEALTHY" if (o.media or o.schema == "V1") else "FLAKY_EVIDENCE"
        if rs == "FLAKY":
            # V1/V2 also call "HTTP-only fallback" FLAKY: require real media.
            return "FLAKY_EVIDENCE" if o.media else "FLAKY_NO_EVIDENCE"
        if rs == "UNKNOWN":
            return "UNKNOWN"
        return failure_class(o.http_code, o.reason, rs)
    if o.schema == "M3U_ONLY":
        if rs in ("HEALTHY", "WORKING_NOW"):
            return "HEALTHY"
        if rs == "FLAKY":
            return "FLAKY_NO_EVIDENCE"
        if rs == "NOT_WORKING_NOW":
            return "UNKNOWN"
        return failure_class("", "", rs)
    return "UNKNOWN"


def is_result_folder(d: Path) -> bool:
    if (d / "results.csv").exists() or (d / "results.json").exists():
        return True
    return any((d / f"{s}.m3u").exists() for s in STATUS_FILES)


def discover_folders(roots: list[Path], exclude: list[Path]) -> list[Path]:
    found = []
    excl = [e.resolve() for e in exclude]
    for root in roots:
        if not root.exists():
            print(f"! input not found: {root}")
            continue
        # case-insensitive order: Windows and Linux must scan folders identically
        for d in sorted([root, *[p for p in root.rglob("*") if p.is_dir()]],
                        key=lambda p: str(p).replace("\\", "/").casefold()):
            rd = d.resolve()
            if any(rd == e or e in rd.parents for e in excl):
                continue
            if ".git" in d.parts:
                continue
            if is_result_folder(d):
                found.append(d)
    # unique, stable
    seen, out = set(), []
    for d in found:
        k = str(d.resolve()).lower()
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def folder_label(d: Path, roots: list[Path]) -> str:
    for r in roots:
        try:
            rel = d.resolve().relative_to(r.resolve())
            s = str(rel).replace("\\", "/")
            return s if s != "." else r.name
        except ValueError:
            continue
    return d.name


def load_folder(d: Path, label: str) -> tuple[list[Obs], dict]:
    rows = read_rows(d)
    schema = detect_schema(rows)
    # Metadata from every M3U in the folder, keyed by URL (ordered lists).
    meta_by_url: dict[str, list[M3UEntry]] = defaultdict(list)
    m3u_status: dict[str, str] = {}
    for p in sorted(d.glob("*.m3u")) + sorted(d.glob("*.m3u8")):
        st = p.stem.upper()
        for e in parse_m3u_file(p):
            meta_by_url[e.url].append(e)
            if st in STATUS_FILES:
                m3u_status.setdefault(e.url, st)

    history = {}
    hp = d / "health_history.json"
    if hp.exists():
        try:
            history = json.loads(hp.read_text(encoding="utf-8-sig"))
        except Exception:
            history = {}

    obs: list[Obs] = []
    if schema == "M3U_ONLY":
        for url, entries in meta_by_url.items():
            for e in entries:
                obs.append(Obs(label, schema, m3u_status.get(url, "UNKNOWN"),
                               e.name, e.attrs.get("group-title", ""), url, meta=e))
    else:
        for r in rows:
            url = clean_ws(r.get("url"))
            if not url:
                continue
            lst = meta_by_url.get(url)
            meta = lst.pop(0) if lst else None
            if lst is not None and not lst and meta is not None:
                meta_by_url[url] = [meta]  # keep last for further duplicates
            o = Obs(folder=label, schema=schema,
                    raw_status=clean_ws(r.get("final_status") or r.get("status")),
                    name=clean_ws(r.get("name")), group=clean_ws(r.get("group")),
                    url=url)
            o.attempts = to_int(r.get("attempts"))
            o.has_video = to_bool(r.get("has_video"))
            o.has_audio = to_bool(r.get("has_audio"))
            o.startup = to_float(r.get("startup_seconds"))
            o.http_code = clean_ws(r.get("http_code"))
            o.reason = clean_ws(r.get("reason") or r.get("detail") or "")
            o.segment_ok = to_bool(r.get("segment_ok"))
            if schema == "V3":
                o.successes = to_int(r.get("successes"))
                o.media = to_bool(r.get("ffprobe_ok"))
                o.played = to_bool(r.get("ffmpeg_ok"))
                # The checker stops early, so the "attempts" column is the
                # configured maximum. Count the tries that really ran.
                tries = len(re.findall(r"\btry \d+:", o.reason))
                if o.raw_status.upper() == "WORKING_NOW":
                    o.attempts = max(o.successes, tries)
                elif tries:
                    o.attempts = tries
            else:
                o.media = o.has_video or o.has_audio
                o.played = False
                o.successes = 1 if o.media else 0
                h = history.get(url) if isinstance(history, dict) else None
                if isinstance(h, dict) and o.media:
                    o.hist_success = to_int(h.get("total_successes"))
                    o.hist_fail = to_int(h.get("total_failures"))
            o.meta = meta
            obs.append(o)
    for o in obs:
        if o.meta and not o.name:
            o.name = o.meta.name
        o.obs_class = classify_obs(o)
    info = {"folder": label, "path": str(d), "schema": schema, "records": len(obs)}
    return obs, info


# ---------------------------------------------------------------------------
# URL policy checks
# ---------------------------------------------------------------------------
CRED_QUERY_KEYS = {"username", "password", "user", "pass", "pwd", "passwd", "login", "mac"}
TOKEN_QUERY_KEYS = {"token", "access_token", "auth_token", "authtoken", "jwt", "wmsauthsign",
                    "hdnts", "hdnea", "hdntl", "auth", "authkey", "key-pair-id", "policy",
                    "secure", "sign", "signature", "expires"}
# Values that are published placeholders, not anyone's private token.
PUBLIC_TOKEN_VALUES = {"", "test", "testpub", "public", "free", "guest", "onlinetv", "none", "0", "1"}
XTREAM_PATH = re.compile(r"/(?:live|movie|series)/([^/]{4,})/([^/]{4,})/(\d{3,})(?:\.\w+)?$", re.I)
XTREAM_BARE = re.compile(r"^/([^/]{4,})/([^/]{4,})/(\d{3,})(?:\.(?:ts|m3u8))?$", re.I)
PATH_WORDS = {"hls", "live", "stream", "streams", "livestream", "playlist", "index", "master",
              "webplayer", "channel", "channels", "tv", "video", "media", "play", "chunklist",
              "smil", "mono", "abr", "edge", "origin", "dash", "out", "v1", "v2", "public"}


URL_SHORTENERS = {"j.mp", "bit.ly", "tinyurl.com", "goo.gl", "cutt.ly", "is.gd", "t.co",
                  "ow.ly", "rebrand.ly", "shorturl.at", "tiny.cc", "rb.gy", "s.id"}


def looks_xtream(path: str, has_port: bool) -> bool:
    m = XTREAM_PATH.search(path) or (XTREAM_BARE.match(path) if has_port else None)
    if not m:
        return False
    user, pwd, sid = m.groups()
    if user.lower() in PATH_WORDS or pwd.lower() in PATH_WORDS:
        return False
    if user == sid or pwd == sid or user.isdigit() and pwd.isdigit():
        return False
    return True


def policy_check(url: str, meta: M3UEntry | None, denylist: list[str]) -> tuple[str, str]:
    """Return (reason, detail) if the URL must not be published, else ('', '')."""
    u = url.strip()
    try:
        p = urllib.parse.urlsplit(u)
    except Exception:
        return "INVALID_URL", "unparseable URL"
    host = (p.hostname or "").lower()
    # denylist first, so a pirate-host URL is always withheld even when malformed
    host_path = host + p.path.lower()
    for d in denylist:
        # "example.com" matches that domain and its subdomains; a bare word
        # ("daddylive") matches anywhere in the host name; "host/path" matches
        # that URL prefix (e.g. one tinyurl alias).
        if "/" in d:
            hit = host_path.startswith(d)
        elif "." in d:
            hit = host == d or host.endswith("." + d)
        else:
            hit = d in host
        if hit:
            return "DENYLIST", f"matches denylist entry '{d}'"
    if not u.lower().startswith(("http://", "https://")) or '"' in u:
        return "INVALID_URL", "not a plain http(s) URL"
    if not host:
        return "INVALID_URL", "no host"
    if p.username or p.password:
        return "CREDENTIALS", "username/password embedded in URL"
    qs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q = {k.lower() for k, _ in qs}
    if q & CRED_QUERY_KEYS and ({"username", "password", "pass", "pwd", "passwd", "mac"} & q):
        return "CREDENTIALS", "account credentials in query string"
    if "get.php" in p.path.lower() or "player_api.php" in p.path.lower() or "stalker_portal" in u.lower():
        return "CREDENTIALS", "Xtream/Stalker panel endpoint"
    if looks_xtream(p.path, p.port is not None):
        return "CREDENTIALS", "Xtream-codes style user/pass path"
    tok = {k.lower() for k, v in qs
           if k.lower() in TOKEN_QUERY_KEYS and v.strip().lower() not in PUBLIC_TOKEN_VALUES}
    if tok:
        return "AUTH_REQUIRED", "private/expiring token in URL: " + ",".join(sorted(tok))
    if meta is not None:
        if meta.cookie:
            return "AUTH_REQUIRED", "requires cookie/authorization header"
        if meta.drm_props:
            return "DRM", "KODIPROP DRM licence in source entry"
    return "", ""


def is_vod(url: str) -> bool:
    # live Icecast radio ends in .mp3/.aac too, so only video files and
    # archive.org downloads count as on-demand files
    p = urllib.parse.urlsplit(url.lower())
    host = p.hostname or ""
    return bool(re.search(r"\.(?:mp4|mkv|avi|mov|wmv|webm|flv)$", p.path)
                or host == "archive.org" or host.endswith(".archive.org"))


def playable_url(url: str) -> str:
    # a few lists carry raw spaces in query strings; players need them encoded
    return url.strip().replace(" ", "%20")


def load_channel_denylist(path: Path | None) -> list[re.Pattern]:
    """Channel-name patterns (regex, case-insensitive) that are never published."""
    out = []
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(re.compile(line, re.I))
    return out


def name_denied(name: str, patterns: list[re.Pattern]) -> str:
    for pat in patterns:
        if pat.search(name or ""):
            return pat.pattern
    return ""


def is_proxied(url: str) -> bool:
    low = url.lower()
    p = urllib.parse.urlsplit(low)
    host = p.hostname or ""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) and ".php" in p.path:
        return True
    return (".php" in p.path or "proxy" in low or "workers.dev" in host
            or "url=http" in low or "restream" in low)


# ---------------------------------------------------------------------------
# Names, regions, keys
# ---------------------------------------------------------------------------
SUPERSCRIPTS = {"ᴴᴰ": " HD", "ᶠᴴᴰ": " FHD", "ᵁᴴᴰ": " UHD", "ˢᴰ": " SD", "ʰᵈ": " HD",
                "ᴿᴬᵂ": " RAW", "ᴸᴵⱽᴱ": " LIVE", "⁴ᴷ": " 4K"}
TAG_WORDS = (
    r"\d{3,4}\s?[pi]|[48]\s?k|uhd|fhd|qhd|hd|sd|hq|lq|hevc|h\.?26[45]|x26[45]|hdr|sdr|"
    r"\d{2,3}\s?fps|backup|bkp|alt|alternative|multi|multi[- ]?audio|raw|vip|premium|"
    r"geo[- ]?blocked|not 24/7|24/7|live|en vivo|ao vivo|direct|test|new|orig|original|"
    r"op\d|opt\d|s\d|server\s?\d|link\s?\d|source\s?\d|mirror\s?\d|\d+\s?mbps|"
    r"low|high|mobile|tv box|hls|dash|m3u8|stream|online"
)
TAG_ONLY_RE = re.compile(rf"^(?:(?:{TAG_WORDS})[\s/|,+-]*)+$", re.I)
TOKEN_TAG_RE = re.compile(
    r"(?<![\w+])(?:\d{3,4}[pi]|[48]k|uhd|fhd|qhd|hd|sd|hevc|h\.?26[45]|hdr|\d{2,3}fps|"
    r"backup|bkp|raw|vip|live|hq|lq)(?![\w+])", re.I)
PREFIX_RE = re.compile(r"^\s*(?:\[([A-Za-z]{2,3})\]|\|([A-Za-z]{2,3})\||([A-Z]{2,3}))\s*[:|]\s*")
PREFIX_DASH_RE = re.compile(r"^\s*([A-Z]{2,3})\s+-\s+")


def strip_symbols(s: str) -> str:
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in "\u200d\ufe0f\ufe0e\u200b\u2060":
            continue
        if cat in ("So", "Sk", "Cs", "Co", "Cn", "Cc", "Cf"):
            out.append(" ")
            continue
        if ch in "★☆•►▶◀»«✓✔✅⚽🔘|*~=_^#@":
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def code_to_region(code: str) -> str:
    c = code.upper()
    c = CODE_ALIASES.get(c, c)
    return c if c in COUNTRY else ""


SOURCE_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—|:]|\bx\b|\bon\b)\s*(?:pluto\s?tv|samsung\s?tv\s?plus|plex|xumo(?:\s?play)?|"
    r"roku|tubi|stirr|local\s?now|lg\s?channels|rakuten\s?tv)\s*$", re.I)


def fold_mtavruli(s: str) -> str:
    # Georgian "Mtavruli" capitals (U+1C90..U+1CBF) -> normal Mkhedruli letters
    out = []
    for ch in s:
        o = ord(ch)
        if 0x1C90 <= o <= 0x1CBA:
            ch = chr(o - 0x1C90 + 0x10D0)
        elif 0x1CBD <= o <= 0x1CBF:
            ch = chr(o - 0x1CBD + 0x10FD)
        out.append(ch)
    return "".join(out)


def clean_name(raw: str) -> tuple[str, str]:
    """Return (display name, region code found in the name)."""
    s = raw or ""
    for k, v in SUPERSCRIPTS.items():
        s = s.replace(k, v)
    s = fold_mtavruli(unicodedata.normalize("NFKC", s))
    region = ""
    m = PREFIX_RE.match(s) or PREFIX_DASH_RE.match(s)
    if m:
        # "IT: Rai 1", "[UK] Sky News", "US | ESPN": an explicit separator makes
        # the code unambiguous, so every ISO code except TV/HD-like words counts.
        code = next(g for g in m.groups() if g)
        reg = code_to_region(code)
        if reg and code.upper() not in NOT_COUNTRY_PREFIX - {"IN", "IT", "NO", "AT", "BE", "MY", "IS"}:
            region = reg
            s = s[m.end():]
    s = strip_symbols(s)

    s = clean_ws(s)
    if re.fullmatch(r"\([^()]+\)", s):  # whole name wrapped: "(DD Sports)"
        s = s[1:-1]
    s = SOURCE_SUFFIX_RE.sub("", s)

    found_regions = []

    def bracket(mo):
        inner = mo.group(1).strip()
        if not inner or TAG_ONLY_RE.match(inner):
            return " "
        # "(Costa Rica)", "(Rusia)", "[UK]": becomes the region, not part of the name
        r = COUNTRY_NAME_TO_CODE.get(inner.casefold()) or (
            code_to_region(inner) if re.fullmatch(r"[A-Za-z]{2,3}", inner) and
            inner.upper() not in NOT_COUNTRY_PREFIX else "")
        if r:
            found_regions.append(r)
            return " "
        return f" ({inner}) " if mo.group(0)[0] == "(" else f" [{inner}] "

    s = re.sub(r"\(([^()]*)\)", bracket, s)
    s = re.sub(r"\[([^\[\]]*)\]", bracket, s)
    s = re.sub(r"\{([^{}]*)\}", " ", s)
    s = TOKEN_TAG_RE.sub(" ", s)
    s = re.sub(r"\(\s*\)|\[\s*\]", " ", s)
    s = re.sub(r"\s*([:|/\-])\s*$", "", s)
    s = clean_ws(s).strip(" -:|.,/")
    s = re.sub(r"^(?:-\s*)+", "", s).strip()
    if re.fullmatch(r"[(\[][^()\[\]]+[)\]]", s):
        s = s[1:-1].strip()
    if found_regions and not region:
        region = found_regions[0]
    return (s or clean_ws(raw)), region


def name_key(display: str) -> str:
    s = unicodedata.normalize("NFKD", display.casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ").replace("+", " plus ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b([^\W\d_]{2,})(\d+)\b", r"\1 \2", s)
    s = clean_ws(s)
    s = re.sub(r"^(?:the )", "", s)
    if s.endswith(" tv") and len(s.split()) > 1:
        s = s[:-3].strip()
    if s.endswith(" channel") and len(s.split()) > 1:
        s = s[:-8].strip()
    return s


GENERIC_NAMES = {"kamera", "live kamera", "kamery", "camera", "webcam", "cam", "live cam",
                 "stream", "channel", "live", "unknown", "untitled", "no name", "noname",
                 "test", "canal", "tv", "radio", "video", "new", "link", "server"}

TVGID_IPTVORG = re.compile(r"^[A-Za-z0-9&+\-_.]+\.([a-z]{2})(?:@[\w\-]+)?$")


def region_from_meta(attrs: dict, group: str) -> str:
    tid = (attrs.get("tvg-id") or "").strip()
    m = TVGID_IPTVORG.match(tid)
    if m:
        r = code_to_region(m.group(1))
        if r:
            return r
    cid = (attrs.get("channel-id") or "").strip().lower()
    m = re.search(r"-([a-z]{2})$", cid)
    if m:
        r = code_to_region(m.group(1))
        if r:
            return r
    tc = (attrs.get("tvg-country") or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", tc):
        r = code_to_region(tc)
        if r:
            return r
    g = clean_ws(strip_symbols(group)).casefold()
    g = re.sub(r"^\d+\.\s*", "", g)
    if g in COUNTRY_NAME_TO_CODE:
        return COUNTRY_NAME_TO_CODE[g]
    return ""


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
SPORT_GROUPS = ["Sports", "Football", "Basketball", "Motorsport", "Combat Sports", "Other Sports"]
GROUP_ORDER = [
    "Georgia", *SPORT_GROUPS, "News", "Movies", "Series", "Entertainment", "Documentary",
    "Kids", "Music", "Lifestyle", "Religious", "Shopping", "FAST TV",
    "Local / Regional", "Georgia Radio", "Radio", "On Demand",
]

GE_NAMES = ("imedi", "rustavi", "mtavari", "pirveli", "postv", "pos tv", "maestro", "palitra",
            "adjara", "ajara", "kavkasia", "obieqtivi", "silk sport", "silk universal",
            "marao", "comedy arkhi", "rioni", "enkibenki", "euronews georgia", "starvision",
            "ertsulovneba", "qartuli", "kartuli", "gdstv", "gds tv", "georgian", "setanta sports georgia",
            "1tv georgia", "first channel georgia", "tv 25", "tv25", "mega tv georgia",
            "gurjaani", "trialeti", "odishi", "egrisi", "argo tv", "dardimandi", "tv formula",
            "formula tv", "tabula", "on.ge", "bm.ge")
GE_SCRIPT = re.compile(r"[\u10A0-\u10FF\u1C90-\u1CBF]")


def kw(text: str, words) -> bool:
    return any(w in text for w in words)


def rx(text: str, pattern: str) -> bool:
    return re.search(pattern, text, re.I) is not None


SPORT_RE = (r"\bsports?\b|\bspor\b|sport\s?tv|sportv|sportsnet|sportitalia|\bespn|\bbein\b|"
            r"\bdazn\b|eurosport|setanta|supersport|\btsn\s?\d|\bfox sports|\bnbc sports|\bcbs sports|"
            r"\bbt sport|\btnt sports|\bsky sports|match!|матч|спорт|deporte|deportiv|esporte|"
            r"desporto|\bfutbol|fútbol|football|soccer|\bnba\b|\bnfl\b|\bnhl\b|\bmlb\b|\bufc\b|"
            r"\bwwe\b|\bf1\b|formula 1|motogp|nascar|\bgolf|tennis|cricket|rugby|snooker|billiard|"
            r"darts|boxing|\bboxe|\bmma\b|\bfight|wrestling|racing|motorsport|motorvision|"
            r"\bcycling|\bski\b|volleyball|handball|basket|hockey|baseball|olympic|\bpoker|"
            r"red bull tv|alkass|\bssc\b|\bad sports|win sports|\btyc\b|stadium|\bfubo|"
            r"rally|indycar|wrc\b|nhra|bellator|\bpfl\b|glory|\bpga\b|\batp\b|\bwta\b|\bufl\b|"
            r"\bmls\b|laliga|la liga|premier league|serie a|bundesliga|ligue 1|uefa|fifa|"
            r"champions league|\bsport|رياض|ספורט")


def sport_subgroup(t: str) -> str:
    if rx(t, r"\bufc\b|\bmma\b|boxing|\bboxe\b|\bbox tv|\bfight|wrestling|\bwwe\b|\bpfl\b|"
             r"bellator|glory|kickbox|combat|one championship|\bdazn boxing|\bk-?1\b|\bfite\b"):
        return "Combat Sports"
    if rx(t, r"horse|sky racing|racing tv|\btvg\b|at the races|racing\.com|trackside|greyhound|"
             r"strongman|billiard|snooker"):
        return "Other Sports"
    if rx(t, r"\bf1\b|formula 1|formula one|motogp|motorsport|motor sport|motorvision|\bmotors? tv|"
             r"nascar|indycar|\bwrc\b|rally|nhra|superbike|\bspeed\b|racing|\bmotor\b|autosport|"
             r"\bdtm\b|supercars"):
        return "Motorsport"
    if rx(t, r"\bnba\b|basket|euroleague|\bwnba\b|\bfiba\b|\bacb\b"):
        return "Basketball"
    if rx(t, r"football|futbol|fútbol|futebol|soccer|calcio|fussball|fußball|laliga|la liga|"
             r"premier league|\bepl\b|serie a|bundesliga|ligue 1|uefa|fifa|champions league|"
             r"\bmutv\b|chelsea|real madrid|\bbar[cç]a\b|liverpool|juventus|milan tv|inter tv|"
             r"benfica|sporting tv|galatasaray|fenerbah|be[sş]ikta[sş]|arsenal|\bbvb\b|\bajax\b|"
             r"\bpsg\b|\bboca\b|river plate|\bzenit\b|\btudn\b|golazo|\bgol\s?tv|goltv|\bgoal\b|"
             r"\bmls\b|eredivisie|copa |liga mx|libertadores|\bfutebol|futsal"):
        return "Football"
    if rx(t, r"tennis|golf|\bpga\b|cricket|rugby|snooker|billiard|\bpool\b|darts|hockey|\bnhl\b|"
             r"\bnfl\b|\bmlb\b|baseball|cycling|\bski|surf|sailing|volleyball|handball|poker|"
             r"esport|e-sport|chess|athletic|olympic|equestrian|swim|badminton|table tennis|"
             r"padel|bowling|pickleball|lacrosse|\batp\b|\bwta\b|\bufl\b|softball|cricket|"
             r"curling|biathlon|triathlon|marathon|fishing tv|\bgaa\b|\bafl\b|\bnrl\b"):
        return "Other Sports"
    return "Sports"


GENRE_RULES = [
    ("Kids", r"\bkids?\b|children|cartoon|animation|animated|junior|\bnick|nickelodeon|disney|"
             r"\bbaby|toons?\b|boomerang|cbeebies|\bkika\b|pbs kids|детск|мульт|infantil|kinder|"
             r"enfants|[cç]ocuk|duronto|\bkidz|\banime\b|peppa|pok[eé]mon|barney|teletubbies|"
             r"enkibenki|მულტ|საბავშვ"),
    ("News", r"\bnews|noticias|notícias|nachrichten|новост|haber|أخبار|\binfo\b|\bcnn\b|bbc news|"
             r"sky news|al jazeera|aljazeera|euronews|france 24|\bdw\b|\bcnbc|bloomberg|business|"
             r"weather|legislative|parliament|c-span|\bnhk world|trt world|cgtn|\bi24|newsmax|"
             r"\babc news|\bcbs news|\bnbc news|actualit|journal|telediario|ახალი ამბ|\bnewsy\b|"
             r"\bmsnbc|\bfox news|\bcourt tv|\blive now|\bwion\b|\bndtv"),
    ("Movies", r"movie|\bfilm|cinema|\bcine|kino|фильм|кино|filmes|pel[ií]culas|\baction\b|"
               r"thriller|horror|western|\bclassic|hallmark|\bamc\b|\bhbo\b|\btcm\b|\bmgm\b|"
               r"\bflix\b|\bfilmbox|\bfilmrise|\bflicks|\bscreen|ფილმ|კინო"),
    ("Series", r"\bseries\b|\bsérie|\bserie\b|tv shows|sitcom|novela|telenovela|сериал|\bdizi\b|"
               r"\bdrama\b|\bdramas\b|სერიალ"),
    ("Documentary", r"documentar|\bdocu|discovery|nat ?geo|national geographic|\bhistory\b|"
                    r"science|nature|animal|\bwild|smithsonian|curiosity|viasat (?:explore|history|nature)|"
                    r"education|\bplanet\b|\bspace\b|\bexplore|познават|документ|belgesel|დოკუმენტ"),
    ("Music", r"\bmusic|m[uú]sica|музык|\bmtv\b|\bvh1\b|\bhits?\b|mezzo|clubbing|karaoke|"
              r"concert|\btrace\b|deluxe music|stingray|muzik|müzik|\bradio\b|\bfm\b|\bjazz|"
              r"\brock\b|\bpop\b|\bclassica|\b4fun|\bbox hits|\bkiss\b|მუსიკ"),
    ("Lifestyle", r"lifestyle|cooking|\bfood|travel|outdoor|\bauto\b|fashion|\bhome\b|garden|"
                  r"relax|hunting|fishing|tastemade|cuisine|\bcars?\b|\bchef|\bkitchen|\bgourmet|"
                  r"\bwellness|\bhealth|\byoga|\bpets?\b|\bdecor|\bstyle\b"),
    ("Religious", r"religio|church|christian|gospel|catholic|islam|quran|coran|bible|\bgod\b|"
                  r"\bfaith|\bjesus|\bhope\b|ewtn|\b3abn|daystar|\btbn\b|\bsalt\b|\bmiracle|"
                  r"\bprayer|ортодокс|православ|\bdini\b|\bhindu|\bsikh|\bbhakti|\bsanskar|\baastha"),
    ("Shopping", r"\bshop|shopping|\bqvc\b|\bhsn\b|teleshop|jewelry|\bhse\b"),
    ("Entertainment", r"entertainment|comedy|family|reality|game show|culture|\bfun\b|\bshow\b|"
                      r"\bgeneral entertainment|\bvariety|\bcomedy|\blaugh|\btalk"),
    ("Local / Regional", r"\blocal|regional|regionales|локальн|региональн|\bcommunity\b|\bcity\b"),
]

FAST_HOST_WORDS = ("jmp2.uk", "pluto.tv", "plutotv", "samsung", "plex.tv", "plex.direct",
                   "roku", "xumo", "stirr", "tubi", "lgchannels", "localnow", "amagi.tv",
                   "wurl", "mediatailor", "rakuten", "redbox", "vizio", "frequency.stream",
                   "glewedtv", "distro.tv", "sofast", "freevee", "whale.tv")
FAST_GROUP_WORDS = ("pluto", "samsung", "plex", "roku", "xumo", "tubi", "stirr", "rakuten",
                    "fast", "local now", "lg channels", "distro")


def group_verdict(g: str) -> str:
    """What a single source group-title says about the channel ('' = nothing)."""
    gl = g.casefold()
    for genre, pat in GENRE_RULES:
        if rx(gl, pat):
            # "LocalNow: Sports", "Sports & News": a sports word wins over the
            # generic container words that GENRE_RULES would also match
            if rx(gl, SPORT_RE) and genre in ("Local / Regional", "Entertainment", "Lifestyle"):
                return "SPORT"
            return genre
    if rx(gl, SPORT_RE):
        return "SPORT"
    return ""


def assign_group(display: str, region: str, groups: list[str], urls: list[str],
                 tvg_ids: list[str], audio_only: bool = False) -> str:
    name_l = display.casefold()
    grp_l = " | ".join(g.casefold() for g in groups if g)
    url_l = " ".join(urls).lower()

    is_ge = (region == "GE"
             or any(TVGID_IPTVORG.match(t or "") and (t or "").lower().split("@")[0].endswith(".ge")
                    for t in tvg_ids)
             or (not region and (GE_SCRIPT.search(display) or kw(grp_l, ("georgia", "ქართ", "грузин",
                                                                        "sakartvelo", "georgian"))
                                 or kw(name_l, GE_NAMES) or ".ge/" in url_l or ".ge:" in url_l)))
    if region and region != "GE":
        is_ge = False
    is_radio = audio_only or (rx(name_l, r"\bradio\b|რადიო|радио") and not rx(name_l, r"\btv\b"))
    if is_radio:
        return "Georgia Radio" if is_ge else "Radio"
    if is_ge:
        return "Georgia"

    # 1) the channel name decides when it is explicit
    if rx(name_l, SPORT_RE):
        # multi-sport brands (ESPN, beIN, CCTV 5) stay in "Sports" even when a
        # source filed them under a football folder
        return sport_subgroup(name_l)
    for g, pat in GENRE_RULES:
        if rx(name_l, pat):
            return g

    # 2) otherwise the sources vote with their group-titles; ties go to a
    #    non-sport genre, because one stray "Sports" folder is a common mislabel
    votes = Counter(v for v in (group_verdict(g) for g in groups) if v)
    if votes:
        best = max(votes.items(), key=lambda kv: (kv[1], kv[0] != "SPORT"))[0]
        if best == "SPORT":
            return sport_subgroup(" | ".join(g.casefold() for g in groups
                                             if group_verdict(g) == "SPORT"))
        return best

    if kw(url_l, FAST_HOST_WORDS) or kw(grp_l, FAST_GROUP_WORDS):
        return "FAST TV"
    if region:
        return f"International | {COUNTRY.get(region, region)}"
    return "International"


# ---------------------------------------------------------------------------
# Candidate URLs and channels
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    url: str
    obs: list[Obs] = field(default_factory=list)
    status: str = ""             # HEALTHY / FLAKY / RESTRICTED / OFFLINE_NOW / DRM / DEAD / UNKNOWN
    accepted: bool = False
    reject_reason: str = ""
    reject_detail: str = ""
    successes: int = 0
    failures: int = 0
    attempts: int = 0
    startup: float = 0.0
    media: bool = False
    played: bool = False
    has_video: bool = False
    has_audio: bool = False
    display: str = ""
    region: str = ""
    key: str = ""
    attrs: dict = field(default_factory=dict)
    user_agent: str = ""
    referer: str = ""
    origin: str = ""
    groups: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    score: tuple = ()
    policy: str = ""             # policy_check verdict, whatever the health status

    @property
    def secret(self) -> bool:
        """URL must not appear in any public file (credentials, tokens, pirate hosts)."""
        return self.policy in ("CREDENTIALS", "AUTH_REQUIRED", "DENYLIST", "DRM", "PAY_CHANNEL")

    @property
    def public_url(self) -> str:
        if not self.secret:
            return self.url
        p = urllib.parse.urlsplit(self.url)
        return f"{p.scheme}://{p.hostname or ''}/[redacted]"

    @property
    def tvg_id(self) -> str:
        return self.attrs.get("tvg-id", "")

    @property
    def logo(self) -> str:
        return self.attrs.get("tvg-logo", "")


def good_logo(u: str) -> bool:
    return bool(u) and u.lower().startswith(("http://", "https://")) and " " not in u and '"' not in u


def tvgid_quality(t: str) -> int:
    if not t:
        return 0
    if TVGID_IPTVORG.match(t):
        return 2
    return 1


def aggregate_candidate(url: str, obs: list[Obs], denylist: list[str],
                        chan_deny: list[re.Pattern] = ()) -> Candidate:
    c = Candidate(url=url, obs=obs)
    classes = Counter(o.obs_class for o in obs)
    c.folders = sorted({o.folder for o in obs})
    c.attempts = sum(max(o.attempts, 1) for o in obs)
    c.successes = sum(o.successes for o in obs) + sum(max(o.hist_success - 1, 0) for o in obs)
    c.failures = max(c.attempts - sum(o.successes for o in obs), 0) + sum(o.hist_fail for o in obs)
    c.media = any(o.media for o in obs)
    c.played = any(o.played for o in obs)
    c.has_video = any(o.has_video for o in obs)
    c.has_audio = any(o.has_audio for o in obs)
    good = [o.startup for o in obs if o.startup > 0 and o.obs_class in ("HEALTHY", "FLAKY_EVIDENCE")]
    c.startup = round(min(good), 2) if good else 0.0

    if classes["HEALTHY"]:
        c.status = "HEALTHY"
    elif classes["FLAKY_EVIDENCE"]:
        c.status = "FLAKY"
    elif classes["FLAKY_NO_EVIDENCE"]:
        c.status = "FLAKY_NO_EVIDENCE"
    else:
        for st in ("DRM", "RESTRICTED", "OFFLINE_NOW", "UNKNOWN", "DEAD"):
            if classes[st]:
                c.status = st
                break
        else:
            c.status = "UNKNOWN"

    # metadata: prefer the observation with the richest M3U entry
    metas = [o.meta for o in obs if o.meta is not None]
    best_meta = max(metas, key=lambda m: (tvgid_quality(m.attrs.get("tvg-id", "")),
                                          good_logo(m.attrs.get("tvg-logo", "")),
                                          len(m.attrs)), default=None)
    attrs = dict(best_meta.attrs) if best_meta else {}
    for m in metas:  # fill holes from other copies of the same URL
        for k in ("tvg-id", "tvg-logo", "tvg-name", "channel-id", "tvg-country"):
            if not attrs.get(k) and m.attrs.get(k):
                attrs[k] = m.attrs[k]
    c.attrs = attrs
    for m in metas:
        c.user_agent = c.user_agent or m.user_agent
        c.referer = c.referer or m.referer
        c.origin = c.origin or m.origin

    # name: most common cleaned name (M3U name beats CSV name, which can be broken)
    names = Counter()
    regions = Counter()
    for o in obs:
        raw = (o.meta.name if o.meta and o.meta.name else o.name) or ""
        if "like Gecko" in raw or 'group-title="' in raw:
            raw = raw.split(",")[-1]
        disp, reg = clean_name(raw)
        if disp:
            names[disp] += 1
        if reg:
            regions[reg] += 1
        c.groups.append(o.meta.attrs.get("group-title", "") if o.meta else o.group)
    c.groups = sorted({g for g in c.groups if g})
    c.display = names.most_common(1)[0][0] if names else (urllib.parse.urlsplit(url).hostname or url)
    meta_region = region_from_meta(attrs, c.groups[0] if c.groups else "")
    c.region = regions.most_common(1)[0][0] if regions else meta_region
    c.key = name_key(c.display) or url
    # Placeholder names ("Live kamera", a bare host name) are shared by unrelated
    # streams in some lists: never merge those by name.
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if c.key in GENERIC_NAMES or c.display.lower() == host or re.fullmatch(r"[\w-]+(\.[\w-]+)+", c.display):
        c.key = "url:" + url
        segs = [re.sub(r"\.(stream|smil|m3u8|ts)$", "", s, flags=re.I)
                for s in urllib.parse.urlsplit(url).path.split("/") if s]
        segs = [s for s in segs if s.lower() not in PATH_WORDS | {"tmr", "livetrans", "chunklist.m3u8",
                                                                   "playlist.m3u8", "index.m3u8",
                                                                   "video.m3u8", "_definst_"}]
        if segs and c.key[4:]:
            c.display = f"{c.display} – {max(segs, key=len)[:24]}"

    if c.status == "HEALTHY":
        c.accepted = True
    elif c.status == "FLAKY":
        hard = classes["DEAD"] > 0 or classes["DRM"] > 0
        ratio = c.successes / max(c.attempts, 1)
        if hard:
            c.reject_reason, c.reject_detail = "FLAKY_REJECTED", "also hard-failed (404/410/DRM) in another run"
        elif ratio < 0.25 and c.attempts > 4:
            c.reject_reason, c.reject_detail = "FLAKY_REJECTED", f"low success ratio {c.successes}/{c.attempts}"
        else:
            c.accepted = True
    elif c.status == "FLAKY_NO_EVIDENCE":
        c.reject_reason, c.reject_detail = "FLAKY_REJECTED", "FLAKY label without any audio/video evidence"
    else:
        c.reject_reason = c.status

    pr, detail = policy_check(url, best_meta, denylist)
    if not pr:
        # the majority name decides: one mislabelled copy must not sink a channel
        pat = name_denied(c.display, chan_deny)
        if pat:
            pr, detail = "PAY_CHANNEL", f"channel name matches denylist pattern '{pat}'"
    c.policy = pr
    if c.accepted and pr:
        c.accepted = False
        c.reject_reason, c.reject_detail = pr, detail
    if not c.reject_detail and not c.accepted:
        c.reject_detail = next((o.reason[:300] for o in obs if o.reason), "")
    return c


def candidate_score(c: Candidate, folder_rel: dict[str, float]) -> tuple:
    ratio = c.successes / max(c.attempts, 1)
    startup = c.startup if c.startup > 0 else 99.0
    meta = tvgid_quality(c.tvg_id) + (1 if good_logo(c.logo) else 0)
    rel = max((folder_rel.get(f, 0) for f in c.folders), default=0)
    return (
        1 if c.status == "HEALTHY" else 0,
        round(ratio, 2),
        min(c.successes, 10),
        -min(c.failures, 20),
        1 if c.played else 0,
        1 if c.media else 0,
        -round(min(startup, 99.0)),
        0 if is_proxied(c.url) else 1,
        0 if (c.user_agent or c.referer) else 1,
        meta,
        round(rel, 2),
        len(c.folders),
    )


@dataclass
class Channel:
    key: str
    region: str
    candidates: list[Candidate]
    display: str = ""
    group: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    logo: str = ""
    notes: list[str] = field(default_factory=list)
    epg_source: str = ""         # guide URL that matched this channel's tvg-id

    @property
    def primary(self) -> Candidate:
        return self.candidates[0]


def build_channels(accepted: list[Candidate], folder_rel: dict[str, float]) -> list[Channel]:
    by_key: dict[str, list[Candidate]] = defaultdict(list)
    for c in accepted:
        by_key[c.key].append(c)

    channels: list[Channel] = []
    for key, cands in by_key.items():
        regs = {c.region for c in cands if c.region}
        # FAST services run the same programming in every country, so their
        # per-country feeds become one channel with the other feeds as backups.
        all_fast = all(kw(c.url.lower(), FAST_HOST_WORDS) for c in cands)
        buckets: dict[str, list[Candidate]] = defaultdict(list)
        for c in cands:
            r = c.region
            if all_fast:
                r = next(iter(regs)) if len(regs) == 1 else ""
            elif not r and len(regs) == 1:
                r = next(iter(regs))  # unknown region joins the only known one
            buckets[r].append(c)
        for reg, lst in buckets.items():
            for c in lst:
                c.score = candidate_score(c, folder_rel)
            lst.sort(key=lambda x: x.score, reverse=True)
            channels.append(Channel(key=key, region=reg, candidates=lst))

    for ch in channels:
        p = ch.primary
        # display name: prefer a name that came with an iptv-org style tvg-id
        named = [c for c in ch.candidates if tvgid_quality(c.tvg_id) == 2]
        ch.display = (named[0].display if named and len(named[0].display) <= len(p.display) + 6
                      else p.display)
        # metadata merge: best stream + best metadata (same channel/region only)
        ch.tvg_id = p.tvg_id
        if not ch.tvg_id:
            for c in ch.candidates:
                if c.tvg_id:
                    ch.tvg_id = c.tvg_id
                    ch.notes.append("tvg-id borrowed from backup")
                    break
        ch.logo = p.logo if good_logo(p.logo) else ""
        if not ch.logo:
            for c in ch.candidates:
                if good_logo(c.logo):
                    ch.logo = c.logo
                    ch.notes.append("logo borrowed from backup")
                    break
        ch.tvg_name = p.attrs.get("tvg-name") or ch.display
        ch.group = assign_group(
            ch.display, ch.region,
            [g for c in ch.candidates for g in c.groups],
            [c.url for c in ch.candidates],
            [c.tvg_id for c in ch.candidates],
            audio_only=(p.has_audio and not any(c.has_video for c in ch.candidates)),
        )
        if all(is_vod(c.url) for c in ch.candidates):
            ch.group = "On Demand"
            ch.notes.append("single media file (VOD), not a live channel")
        if p.status == "FLAKY":
            ch.notes.append("primary is FLAKY-with-evidence (no HEALTHY copy found)")
        if is_proxied(p.url):
            ch.notes.append("primary goes through a proxy/PHP relay")
        if p.user_agent or p.referer:
            ch.notes.append("primary needs custom User-Agent/Referer (sent via #EXTVLCOPT)")

    # tiny country folders are noise on a TV remote: fold them into "International"
    sizes = Counter(ch.group for ch in channels)
    for ch in channels:
        if ch.group.startswith("International | ") and sizes[ch.group] < MIN_COUNTRY_GROUP:
            ch.group = "International"

    # make visible names unique: add region, then a counter
    by_disp: dict[str, list[Channel]] = defaultdict(list)
    for ch in channels:
        by_disp[ch.display.casefold()].append(ch)
    for lst in by_disp.values():
        if len(lst) < 2:
            continue
        for ch in lst:
            if ch.region:
                ch.display = f"{ch.display} ({COUNTRY.get(ch.region, ch.region)})"
    by_disp.clear()
    for ch in channels:
        by_disp[ch.display.casefold()].append(ch)
    for lst in by_disp.values():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda c: c.primary.score, reverse=True)
        for i, ch in enumerate(lst[1:], start=2):
            ch.display = f"{ch.display} {i}"
    return channels


# ---------------------------------------------------------------------------
# EPG matching
# ---------------------------------------------------------------------------
# Georgian guides name channels in Georgian script ("იმედი", "რუსთავი 2");
# playlists use Latin ("Imedi TV", "Rustavi 2"). Both sides are reduced to the
# same loose Latin key before comparing.
GE_TRANSLIT = dict(zip(
    "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ",
    ["a", "b", "g", "d", "e", "v", "z", "t", "i", "k", "l", "m", "n", "o", "p", "zh", "r",
     "s", "t", "u", "p", "k", "gh", "k", "sh", "ch", "ts", "dz", "ts", "ch", "kh", "j", "h"]))
EPG_ALIASES = {"1tv": "pirveli arkhi", "1 arkhi": "pirveli arkhi",
               "1 arkhi sport": "pirveli arkhi sporti", "regbi": "ragbi"}
EPG_SKIP_GROUPS = {"Radio", "Georgia Radio", "On Demand"}  # never give these a TV schedule by name


def epg_key(name: str) -> str:
    s = "".join(GE_TRANSLIT.get(ch, ch) for ch in fold_mtavruli(name or ""))
    s = re.sub(r"\.[a-z]{2}$", "", s.strip(), flags=re.I)   # guide ids end in ".ge" etc.
    k = name_key(clean_name(s)[0])
    for a, b in (("f", "p"), ("x", "kh"), ("q", "k"), ("w", "v"), ("c", "k")):
        k = k.replace(a, b)   # Formula/Pormula, Arxi/Arkhi, Obieqtivi/Obiektivi
    k = " ".join(re.sub(r"\b(tv|hd|channel)\b", " ", k).split())
    return EPG_ALIASES.get(k, k)


def load_epg_sources(path: Path | None) -> list[str]:
    if path and path.exists():
        urls = [ln.split("#", 1)[0].strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        return [u for u in urls if u]
    return list(EPG_URLS)


def load_guides(urls: list[str]) -> list[dict]:
    guides = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            text = data.decode("utf-8", errors="replace")
        except Exception as ex:
            print(f"  ! EPG guide not loaded: {url} ({type(ex).__name__}: {ex})")
            continue
        # ids as the player sees them after XML parsing ("&amp;" -> "&")
        ids = {html.unescape(i) for i in re.findall(r'<channel id="([^"]+)"', text)}
        names: dict[str, set[str]] = defaultdict(set)
        for cid, disp in re.findall(r'<channel id="([^"]+)"[^>]*>\s*<display-name[^>]*>([^<]*)<', text):
            cid = html.unescape(cid)
            names[epg_key(html.unescape(disp))].add(cid)
            names[epg_key(cid)].add(cid)
        for cid in ids:
            names[epg_key(cid)].add(cid)
        print(f"  EPG {len(ids):>6} channels  {len(data) / 1e6:6.1f} MB  {url}")
        guides.append({"url": url, "ids": ids, "names": names})
    return guides


def match_epg(channels: list[Channel], guides: list[dict]) -> Counter:
    """Point each channel's tvg-id at a guide that really carries it.
    Order: exact tvg-id, tvg-id without the iptv-org '@feed' suffix, then a
    unique loose-name match (Georgian transliterated) in a compatible guide."""
    used = Counter()
    for ch in channels:
        tids = [t for t in [ch.tvg_id, *(c.tvg_id for c in ch.candidates)] if t]
        hit = None
        for g in guides:
            hit = next((t for t in tids if t in g["ids"]), None)
            if hit:
                break
        if not hit:
            for g in guides:
                hit = next((t.split("@")[0] for t in tids if t.split("@")[0] in g["ids"]), None)
                if hit:
                    break
        if not hit and ch.group not in EPG_SKIP_GROUPS:
            key = epg_key(ch.primary.display)
            for g in guides:
                found = g["names"].get(key) or set()
                # a guide id like "ESPN.us" must agree with a known channel region
                ok = [cid for cid in found if not ch.region or ch.region == "GE"
                      or not re.search(r"\.[a-z]{2}$", cid)
                      or cid.lower().endswith(("." + ch.region.lower(), ".ge"))]
                if len(ok) == 1:
                    hit = ok[0]
                    break
        if hit:
            if hit != ch.tvg_id:
                ch.notes.append(f"tvg-id set from guide (was '{ch.tvg_id}')")
            ch.tvg_id = hit
            ch.epg_source = g["url"]
            used[g["url"]] += 1
    return used


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def attr_safe(v: str) -> str:
    # commas inside attributes confuse simple M3U parsers
    return clean_ws(v.replace('"', "'").replace(",", " "))


def url_attr_safe(v: str) -> str:
    return clean_ws(v).replace('"', "%22").replace(",", "%2C").replace(" ", "%20")


def name_safe(v: str) -> str:
    # AquaPlay splits on the first comma outside quotes; keep names comma-free-safe
    return clean_ws(v).replace("\n", " ").replace('"', "'")


def group_sort_key(g: str) -> tuple:
    if g in GROUP_ORDER:
        return (0, GROUP_ORDER.index(g), "")
    if g.startswith("International | "):
        return (1, 0, g.casefold())
    if g == "International":
        return (2, 0, "")
    return (3, 0, g.casefold())


def extinf_line(display, group, tvg_id="", tvg_name="", logo="") -> str:
    parts = ["#EXTINF:-1"]
    if tvg_id:
        parts.append(f'tvg-id="{attr_safe(tvg_id)}"')
    if tvg_name:
        parts.append(f'tvg-name="{attr_safe(tvg_name)}"')
    if logo:
        parts.append(f'tvg-logo="{url_attr_safe(logo)}"')
    parts.append(f'group-title="{attr_safe(group)}"')
    return " ".join(parts) + "," + name_safe(display)


def header_lines(c: Candidate) -> list[str]:
    out = []
    if c.user_agent:
        out.append(f"#EXTVLCOPT:http-user-agent={c.user_agent}")
    if c.referer:
        out.append(f"#EXTVLCOPT:http-referrer={c.referer}")
    return out


def write_playlist(path: Path, channels: list[Channel], alternates: bool,
                   epg_urls: list[str] = EPG_URLS) -> int:
    """alternates=True: every working stream, the other copies of a channel as
    "Name (alt 2)", "(alt 3)" ... right under it. False: one entry per channel."""
    guides = ",".join(epg_urls)
    lines = [f'#EXTM3U x-tvg-url="{guides}" url-tvg="{guides}"' if guides else "#EXTM3U"]
    n = 0
    for ch in sorted(channels, key=lambda c: (group_sort_key(c.group), c.display.casefold())):
        for i, c in enumerate(ch.candidates if alternates else ch.candidates[:1], start=1):
            name = ch.display if i == 1 else f"{ch.display} (alt {i})"
            lines.append(extinf_line(name, ch.group, ch.tvg_id, ch.tvg_name, ch.logo))
            lines.extend(header_lines(c))
            lines.append(playable_url(c.url))
            n += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return n


def write_recheck(path: Path, cands: list[Candidate]):
    lines = ["#EXTM3U"]
    for c in sorted(cands, key=lambda c: c.display.casefold()):
        grp = (c.groups[0] if c.groups else "Recheck")
        lines.append(extinf_line(c.display, grp, c.tvg_id, "", c.logo if good_logo(c.logo) else ""))
        lines.extend(header_lines(c))
        lines.append(playable_url(c.url))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def validate_m3u(path: Path) -> list[str]:
    errors = []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as ex:
        return [f"not valid UTF-8: {ex}"]
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("file has a UTF-8 BOM")
    lines = text.split("\n")
    if not lines or not lines[0].startswith("#EXTM3U"):
        errors.append("first line is not #EXTM3U")
    urls = Counter()
    pending = None
    n = 0
    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        if line.startswith("#EXTINF"):
            if pending is not None:
                errors.append(f"line {pending}: #EXTINF without URL")
            pending = i
            n += 1
            if line.count('"') % 2:
                errors.append(f"line {i}: unbalanced quotes")
            _, nm = split_extinf(line)
            if not nm:
                errors.append(f"line {i}: empty channel name")
            continue
        if line.startswith("#EXTVLCOPT:"):
            if pending is None:
                errors.append(f"line {i}: #EXTVLCOPT outside an entry")
            continue
        if line.startswith("#"):
            errors.append(f"line {i}: unexpected directive {line[:30]}")
            continue
        if pending is None:
            errors.append(f"line {i}: URL without #EXTINF")
        if not line.lower().startswith(("http://", "https://")) or " " in line:
            errors.append(f"line {i}: malformed URL")
        urls[line] += 1
        pending = None
    if pending is not None:
        errors.append(f"line {pending}: last #EXTINF has no URL")
    dups = [u for u, k in urls.items() if k > 1]
    if dups:
        errors.append(f"{len(dups)} duplicate exact URLs, e.g. {dups[0]}")
    if n != sum(urls.values()):
        errors.append(f"entry count {n} != url count {sum(urls.values())}")
    return errors


# ---------------------------------------------------------------------------
# Optional smoke test
# ---------------------------------------------------------------------------
def find_ffprobe() -> str | None:
    p = shutil.which("ffprobe")
    if p:
        return p
    for c in (r"C:\ffmpeg\bin\ffprobe.exe", r"C:\Program Files\ffmpeg\bin\ffprobe.exe"):
        if Path(c).exists():
            return c
    return None


def smoke_one(ffprobe: str, ch: Channel, timeout: int) -> dict:
    c = ch.primary
    cmd = [ffprobe, "-v", "error", "-user_agent", c.user_agent or DEFAULT_UA,
           "-rw_timeout", str(timeout * 1_000_000)]
    hdr = ""
    if c.referer:
        hdr += f"Referer: {c.referer}\r\n"
    if c.origin:
        hdr += f"Origin: {c.origin}\r\n"
    if hdr:
        cmd += ["-headers", hdr]
    cmd += ["-show_entries", "stream=codec_type", "-of", "json", c.url]
    ok, detail = False, ""
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=timeout + 5,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        streams = (json.loads(cp.stdout or "{}").get("streams") or []) if cp.returncode == 0 else []
        ok = any(s.get("codec_type") in ("video", "audio") for s in streams)
        detail = "media detected" if ok else clean_ws(cp.stderr)[-200:] or "no A/V streams"
    except subprocess.TimeoutExpired:
        detail = "timeout"
    except Exception as ex:
        detail = f"{type(ex).__name__}: {ex}"
    return {"channel": ch.display, "group": ch.group, "url": c.url,
            "smoke_ok": ok, "detail": detail}


def smoke_test(channels: list[Channel], n: int, timeout: int = 15) -> list[dict]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        print("  smoke test skipped: ffprobe not found")
        return []
    def h(ch):  # deterministic pseudo-random order
        return hashlib.md5(ch.primary.url.encode()).hexdigest()
    geo = sorted([c for c in channels if c.group == "Georgia"], key=h)
    sport = sorted([c for c in channels if c.group in SPORT_GROUPS], key=h)
    rest = sorted([c for c in channels if c.group != "Georgia" and c.group not in SPORT_GROUPS], key=h)
    pick = geo[: n // 3] + sport[: n // 3]
    pick += rest[: max(n - len(pick), 0)]
    print(f"  smoke-testing {len(pick)} primary URLs with ffprobe ...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(lambda ch: smoke_one(ffprobe, ch, timeout), pick))


# ---------------------------------------------------------------------------
# Sanitized copy of the result folders (for the GitHub repo / Actions rebuild)
# ---------------------------------------------------------------------------
PUBLISHABLE_REASONS = {"", "OTHER"}


def filter_m3u_text(text: str, keep_url) -> str:
    out, block = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTM3U"):
            out.append(line)
            continue
        if line.startswith("#"):
            block.append(line)
            continue
        secret = any(
            (b.upper().startswith("#KODIPROP") and "license" in b.lower())
            or (b.upper().startswith(("#EXTVLCOPT", "#EXTHTTP"))
                and ("cookie" in b.lower() or "authorization" in b.lower()))
            for b in block)
        extinf = next((b for b in block if b.upper().startswith("#EXTINF")), "")
        name = split_extinf(extinf)[1] if extinf else ""
        if keep_url(line, name) and not secret:
            out.extend(block)
            out.append(line)
        block = []
    if not out or not out[0].upper().startswith("#EXTM3U"):
        out.insert(0, "#EXTM3U")
    return "\n".join(out) + "\n"


def export_health_data(folders: list[Path], roots: list[Path], dest: Path, denylist: list[str],
                       withheld_urls: set[str] = frozenset()):
    """Copy result folders without anything that must not be public: URLs with
    credentials, private tokens, denylisted hosts, pay channels (withheld_urls,
    decided by the build), DRM licence lines, cookies."""
    def keep_url(u: str, name: str = "") -> bool:
        if u in withheld_urls:
            return False
        return policy_check(u, None, denylist)[0] in PUBLISHABLE_REASONS

    dest.mkdir(parents=True, exist_ok=True)
    total_in = total_out = 0
    for d in folders:
        label = folder_label(d, roots)
        target = dest / label
        target.mkdir(parents=True, exist_ok=True)
        rows = read_rows(d)
        kept = [r for r in rows if keep_url(clean_ws(r.get("url")), clean_ws(r.get("name")))]
        total_in += len(rows)
        total_out += len(kept)
        if rows:
            fields = list(rows[0].keys())
            with (target / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
                w.writeheader()
                # full reason text: the try-count and failure class are parsed from it
                w.writerows(kept)
        for p in sorted(d.glob("*.m3u")):
            text = p.read_text(encoding="utf-8-sig", errors="replace")
            (target / p.name).write_text(filter_m3u_text(text, keep_url), encoding="utf-8",
                                         newline="\n")
        hp = d / "health_history.json"
        if hp.exists():
            try:
                hist = json.loads(hp.read_text(encoding="utf-8-sig"))
                hist = {u: v for u, v in hist.items()
                        if keep_url(u, (v or {}).get("name", "") if isinstance(v, dict) else "")}
                (target / "health_history.json").write_text(
                    json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as ex:
                print(f"  ! {hp}: {ex}")
        print(f"  exported {label:<40} {len(kept):>6}/{len(rows):<6} rows")
    print(f"Exported {total_out}/{total_in} records to {dest} "
          f"({total_in - total_out} withheld: credentials/tokens/denylist)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_denylist(path: Path | None) -> list[str]:
    """The file is the source of truth when present; the built-in list is the fallback."""
    if path and path.exists():
        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                items.append(line)
        return sorted(set(items))
    return sorted(set(BUILTIN_DENYLIST))


def main():
    ap = argparse.ArgumentParser(description="Build the consolidated V3 IPTV playlist")
    ap.add_argument("--input", action="append", required=True,
                    help="Root folder holding result folders (repeatable)")
    ap.add_argument("--output", default=".", help="Where to write the ALL_WORKING_TV_V3* files")
    ap.add_argument("--denylist", default="config/denylist_hosts.txt",
                    help="Extra hosts to never publish (one per line)")
    ap.add_argument("--channel-denylist", default="config/denylist_channels.txt",
                    help="Channel-name regex patterns never published (pay channels)")
    ap.add_argument("--smoke", type=int, default=0, help="ffprobe-test N primary URLs at the end")
    ap.add_argument("--min-channels", type=int, default=100,
                    help="Abort without writing if fewer channels than this are found")
    ap.add_argument("--export-health-data", default="",
                    help="Also write a sanitized copy of all result folders here "
                         "(credential/token/denylisted URLs removed) for the repo")
    ap.add_argument("--epg-sources", default="config/epg_sources.txt",
                    help="EPG guide URLs, one per line, in priority order")
    ap.add_argument("--no-epg", action="store_true",
                    help="Skip downloading guides; keep tvg-ids as found and list all guides")
    args = ap.parse_args()

    roots = [Path(p) for p in args.input]
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    denylist = load_denylist(Path(args.denylist) if args.denylist else None)
    chan_deny = load_channel_denylist(Path(args.channel_denylist) if args.channel_denylist else None)

    print("Scanning result folders ...")
    export_dir = Path(args.export_health_data) if args.export_health_data else None
    folders = discover_folders(roots, exclude=[export_dir] if export_dir else [])
    all_obs: list[Obs] = []
    folder_info = []
    for d in folders:
        label = folder_label(d, roots)
        obs, info = load_folder(d, label)
        print(f"  {label:<40} {info['schema']:<9} {info['records']:>6} records")
        all_obs.extend(obs)
        folder_info.append(info)
    if not all_obs:
        raise SystemExit("No result records found.")

    by_url: dict[str, list[Obs]] = defaultdict(list)
    for o in all_obs:
        by_url[o.url].append(o)

    cands = [aggregate_candidate(u, lst, denylist, chan_deny) for u, lst in by_url.items()]
    accepted = [c for c in cands if c.accepted]
    if export_dir:
        export_health_data(folders, roots, export_dir, denylist,
                           {c.url for c in cands if c.secret})

    # folder reliability = share of its URLs that ended up accepted
    f_total, f_ok = Counter(), Counter()
    for c in cands:
        for f in c.folders:
            f_total[f] += 1
            if c.accepted:
                f_ok[f] += 1
    folder_rel = {f: f_ok[f] / f_total[f] for f in f_total}

    channels = build_channels(accepted, folder_rel)
    if len(channels) < args.min_channels:
        raise SystemExit(f"Only {len(channels)} channels built (< --min-channels); nothing written.")

    # cap backups; overflow becomes DUPLICATE_LOWER_QUALITY
    overflow: list[tuple[Channel, Candidate]] = []
    for ch in channels:
        if len(ch.candidates) > MAX_BACKUPS + 1:
            for c in ch.candidates[MAX_BACKUPS + 1:]:
                overflow.append((ch, c))
            ch.candidates = ch.candidates[: MAX_BACKUPS + 1]

    # ---------------- EPG ----------------
    epg_urls = load_epg_sources(Path(args.epg_sources) if args.epg_sources else None)
    epg_used: Counter = Counter()
    if not args.no_epg:
        print("Loading EPG guides ...")
        guides = load_guides(epg_urls)
        if guides:
            epg_used = match_epg(channels, guides)
            epg_urls = [u for u in epg_urls if epg_used[u]]  # only guides that help

    # ---------------- outputs ----------------
    m3u_path = out / f"{OUT_BASE}.m3u"
    if m3u_path.name in PROTECTED_FILES:
        raise SystemExit("refusing to overwrite a protected file")
    n_entries = write_playlist(m3u_path, channels, alternates=True, epg_urls=epg_urls)
    clean_path = out / f"{OUT_BASE}_CLEAN.m3u"
    write_playlist(clean_path, channels, alternates=False, epg_urls=epg_urls)
    subset_paths = []
    subset_counts = {}
    for suffix, wanted in SUBSET_PLAYLISTS.items():
        p = out / f"{OUT_BASE}_{suffix}.m3u"
        subset_counts[suffix] = write_playlist(
            p, [ch for ch in channels if ch.group in wanted], alternates=True, epg_urls=epg_urls)
        subset_paths.append(p)

    def cand_info(c: Candidate) -> dict:
        return {
            "url": c.url, "status": c.status, "sources": c.folders,
            "successes": c.successes, "failures": c.failures, "attempts": c.attempts,
            "startup_seconds": c.startup, "ffprobe_media": c.media, "ffmpeg_played": c.played,
            "proxied": is_proxied(c.url), "user_agent": c.user_agent, "referer": c.referer,
            "origin": c.origin, "source_name": c.display, "tvg_id": c.tvg_id,
        }

    rows = []
    backups = {}
    for ch in sorted(channels, key=lambda c: (group_sort_key(c.group), c.display.casefold())):
        p = ch.primary
        bks = ch.candidates[1:]
        rows.append({
            "channel_name": ch.display, "normalized_name": ch.key, "region": ch.region,
            "group": ch.group, "primary_url": p.url, "primary_source": p.folders[0],
            "all_primary_sources": "; ".join(p.folders), "status": p.status,
            "startup_seconds": p.startup, "success_count": p.successes,
            "failure_count": p.failures, "attempts": p.attempts,
            "backup_count": len(bks),
            "backup_sources": "; ".join(sorted({f for b in bks for f in b.folders})),
            "tvg_id": ch.tvg_id, "epg_guide": ch.epg_source, "tvg_name": ch.tvg_name, "logo": ch.logo,
            "user_agent": p.user_agent, "referer": p.referer,
            "notes": "; ".join(ch.notes),
        })
        if bks:
            backups[f"{ch.display} || {ch.group}"] = {
                "channel_name": ch.display, "normalized_name": ch.key, "region": ch.region,
                "group": ch.group,
                "primary": cand_info(p),
                "backups": [dict(rank=i, **cand_info(b)) for i, b in enumerate(bks, start=1)],
            }

    with (out / f"{OUT_BASE}_SOURCES.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (out / f"{OUT_BASE}_SOURCES.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{OUT_BASE}_BACKUPS.json").write_text(
        json.dumps(backups, ensure_ascii=False, indent=1), encoding="utf-8")

    excluded_rows = []
    for c in sorted((c for c in cands if not c.accepted), key=lambda c: (c.reject_reason, c.display)):
        for o in c.obs:
            detail = (c.reject_detail or o.reason)[:300]
            if c.secret:
                detail = detail.replace(c.url, c.public_url)
                if c.reject_reason != c.policy:
                    detail += f" | URL withheld: {c.policy}"
            excluded_rows.append({
                "channel": c.display, "source": o.folder, "url": c.public_url,
                "reason": c.reject_reason or "OTHER", "observed_status": o.raw_status,
                "http_code": o.http_code, "detail": detail,
            })
    for ch, c in overflow:
        excluded_rows.append({
            "channel": c.display, "source": "; ".join(c.folders), "url": c.url,
            "reason": "DUPLICATE_LOWER_QUALITY", "observed_status": c.status, "http_code": "",
            "detail": f"working, but ranked below {MAX_BACKUPS} backups of '{ch.display}'",
        })
    with (out / f"{OUT_BASE}_EXCLUDED.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["channel", "source", "url", "reason",
                                          "observed_status", "http_code", "detail"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(excluded_rows)

    restricted = [c for c in cands if not c.accepted and c.reject_reason == "RESTRICTED" and not c.policy]
    offline = [c for c in cands if not c.accepted and c.reject_reason == "OFFLINE_NOW" and not c.policy]
    write_recheck(out / f"{OUT_BASE}_RECHECK_RESTRICTED.m3u", restricted)
    write_recheck(out / f"{OUT_BASE}_RECHECK_OFFLINE_NOW.m3u", offline)

    errors = validate_m3u(m3u_path) + [f"CLEAN: {e}" for e in validate_m3u(clean_path)]
    for p in subset_paths:
        errors += [f"{p.name}: {e}" for e in validate_m3u(p)]

    smoke = smoke_test(channels, args.smoke) if args.smoke > 0 else []
    if smoke:
        with (out / f"{OUT_BASE}_SMOKE.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(smoke[0].keys()), lineterminator="\n")
            w.writeheader()
            w.writerows(smoke)

    # ---------------- report ----------------
    obs_by_class = Counter(o.obs_class for o in all_obs)
    url_status = Counter(c.status for c in cands)
    reasons = Counter(c.reject_reason for c in cands if not c.accepted)
    flaky_acc = sum(1 for c in cands if c.status == "FLAKY" and c.accepted)
    flaky_rej = sum(1 for c in cands if c.status in ("FLAKY", "FLAKY_NO_EVIDENCE") and
                    c.reject_reason == "FLAKY_REJECTED")
    healthy_acc = sum(1 for c in cands if c.status == "HEALTHY" and c.accepted)
    dup_url_obs = len(all_obs) - len(by_url)
    dup_channel = len(accepted) - len(channels)
    groups = Counter(ch.group for ch in channels)
    with_bk = sum(1 for ch in channels if len(ch.candidates) > 1)

    per_folder = defaultdict(Counter)
    prim_urls = {ch.primary.url for ch in channels}
    bk_urls = {c.url for ch in channels for c in ch.candidates[1:]}
    for c in cands:
        for f in c.folders:
            per_folder[f]["urls"] += 1
            if c.accepted:
                per_folder[f]["accepted"] += 1
            if c.url in prim_urls:
                per_folder[f]["primary"] += 1
            if c.url in bk_urls:
                per_folder[f]["backup"] += 1

    L = []
    L.append("ALL WORKING TV V3 - CONSOLIDATED BUILD REPORT")
    L.append("=" * 46)
    L.append(f"Built (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
    L.append(f"Inputs: {', '.join(r.name or str(r) for r in roots)}")  # no local paths in public files
    L.append("")
    L.append(f"TOTAL RESULT FOLDERS SCANNED   {len(folder_info):>8}")
    L.append(f"TOTAL INPUT RECORDS            {len(all_obs):>8}")
    L.append(f"UNIQUE URLs                    {len(by_url):>8}")
    L.append("")
    L.append("URL-level status (one verdict per unique URL, all runs combined)")
    L.append(f"  HEALTHY (accepted)           {healthy_acc:>8}")
    L.append(f"  HEALTHY but policy-excluded  {url_status['HEALTHY'] - healthy_acc:>8}")
    L.append(f"  FLAKY ACCEPTED               {flaky_acc:>8}")
    L.append(f"  FLAKY REJECTED               {flaky_rej:>8}")
    for st in ("RESTRICTED", "OFFLINE_NOW", "DRM", "DEAD", "UNKNOWN"):
        L.append(f"  {st:<28} {url_status[st]:>8}")
    L.append("")
    L.append("Policy exclusions (would have played, never published)")
    for r in ("CREDENTIALS", "AUTH_REQUIRED", "DENYLIST", "PAY_CHANNEL", "INVALID_URL", "DRM", "OTHER"):
        n = sum(1 for c in cands if c.reject_reason == r and c.status in ("HEALTHY", "FLAKY"))
        L.append(f"  {r:<28} {n:>8}")
    L.append("")
    L.append(f"DUPLICATE URL RECORDS MERGED   {dup_url_obs:>8}")
    L.append(f"DUPLICATE CHANNELS -> BACKUPS  {dup_channel:>8}")
    L.append(f"DUPLICATES BEYOND BACKUP CAP   {len(overflow):>8}")
    L.append(f"UNIQUE FINAL CHANNELS          {len(channels):>8}")
    L.append(f"PLAYLIST ENTRIES (main .m3u)   {n_entries:>8}   every working stream; extra copies as 'Name (alt N)'")
    L.append(f"CLEAN PLAYLIST ENTRIES         {len(channels):>8}   {OUT_BASE}_CLEAN.m3u, one per channel")
    for suffix, n in subset_counts.items():
        L.append(f"{(suffix + ' PLAYLIST ENTRIES'):<30} {n:>8}   {OUT_BASE}_{suffix}.m3u")
    L.append(f"CHANNELS WITH BACKUPS          {with_bk:>8}")
    n_epg = sum(1 for ch in channels if ch.epg_source)
    L.append(f"CHANNELS WITH TV GUIDE (EPG)   {n_epg:>8}   "
             + ("(guides not checked: --no-epg)" if args.no_epg else f"of {len(channels)}"))
    for u, n in epg_used.most_common():
        L.append(f"     {n:>6}  {u}")
    ge_total = sum(1 for ch in channels if ch.group == "Georgia")
    L.append(f"GEORGIA CHANNELS WITH EPG      {sum(1 for ch in channels if ch.group == 'Georgia' and ch.epg_source):>8}   of {ge_total}")
    L.append(f"PRIMARY = HEALTHY              {sum(1 for ch in channels if ch.primary.status == 'HEALTHY'):>8}")
    L.append(f"PRIMARY = FLAKY (evidence)     {sum(1 for ch in channels if ch.primary.status == 'FLAKY'):>8}")
    L.append("")
    L.append("Channels by group")
    main_groups = [g for g in GROUP_ORDER if groups[g]]
    for g in main_groups:
        L.append(f"  {g:<36} {groups[g]:>6}")
    intl = sorted((g for g in groups if g.startswith("International")), key=lambda g: -groups[g])
    L.append(f"  {'International (all countries)':<36} {sum(groups[g] for g in intl):>6}")
    for g in intl:
        L.append(f"     {g:<33} {groups[g]:>6}")
    other = [g for g in groups if g not in GROUP_ORDER and not g.startswith("International")]
    for g in other:
        L.append(f"  {g:<36} {groups[g]:>6}")
    L.append(f"  {'Sports total (all sport groups)':<36} "
             f"{sum(groups[g] for g in SPORT_GROUPS):>6}")
    L.append("")
    L.append("Per result folder")
    L.append(f"  {'folder':<36} {'schema':<9} {'records':>7} {'urls':>6} {'ok':>6} {'primary':>7} {'backup':>6}")
    for info in folder_info:
        pf = per_folder[info["folder"]]
        L.append(f"  {info['folder']:<36} {info['schema']:<9} {info['records']:>7} {pf['urls']:>6} "
                 f"{pf['accepted']:>6} {pf['primary']:>7} {pf['backup']:>6}")
    L.append("")
    L.append("Exclusion reasons (unique URLs)")
    for r, n in reasons.most_common():
        L.append(f"  {r:<28} {n:>8}")
    L.append("")
    L.append("Observation classes (raw records)")
    for k, n in obs_by_class.most_common():
        L.append(f"  {k:<28} {n:>8}")
    L.append("")
    L.append("M3U VALIDATION: " + ("PASSED" if not errors else f"{len(errors)} problem(s)"))
    for e in errors[:20]:
        L.append(f"  - {e}")
    if smoke:
        ok = sum(1 for s in smoke if s["smoke_ok"])
        L.append("")
        L.append(f"FINAL SMOKE TEST (ffprobe, this machine): {ok}/{len(smoke)} primary URLs answered")
        for s in smoke:
            if not s["smoke_ok"]:
                L.append(f"  fail: {s['channel']} [{s['group']}] - {s['detail'][:90]}")
    L.append("")
    L.append("Rules")
    L.append("  Accepted = V3 WORKING_NOW (2/3 ffprobe+5s ffmpeg playback), V1/V2 HEALTHY,")
    L.append("  or FLAKY with real evidence (>=1 successful playback / ffprobe A/V detected)")
    L.append("  and no 404/410/DRM verdict in any other run.")
    L.append("  Never published: DRM, credentials (user/pass, Xtream/Stalker), private or")
    L.append("  expiring tokens/cookies, denylisted pirate-restream hosts (config/denylist_hosts.txt),")
    L.append("  pay channels by name (config/denylist_channels.txt).")
    L.append("  Same channel from several sources: best-ranked URL is the main entry; the other")
    L.append(f"  working copies follow as 'Name (alt N)' and are ranked in {OUT_BASE}_BACKUPS.json.")
    L.append("  Single media files (VOD) are kept in the 'On Demand' group.")
    L.append("  Results are point-in-time from the tester's PC; streams can change at any time.")
    (out / f"{OUT_BASE}_REPORT.txt").write_text("\n".join(L) + "\n", encoding="utf-8")

    print("\n".join(L))
    if errors:
        raise SystemExit("M3U validation failed (see report)")


if __name__ == "__main__":
    main()
