"""The download brake, /folgen, /zufall and /filmabend: the parts without Discord or network, so they can be tested.

- Brake: below a free-space limit the bot pauses the running downloads in qBittorrent, and starts them again
  once there is the limit plus a margin free. Requests are blocked earlier (MIN_FREE_GB), but downloads that
  already run would otherwise fill the disk.
- /folgen: single episodes instead of whole seasons ("S23E18-E25", "1155-1170"), straight in Sonarr.
- /zufall: a movie or show from the library that wasn't watched yet.
- /filmabend: a Discord event for watching together with Jellyfin's SyncPlay.
- After watching: ask the admins whether to delete a movie or the watched episodes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .core import GB, parse_time, shorten, size_text
from .store import WatchQuestion

# --- download brake ------------------------------------------------------------------------
# qBittorrent states in which a torrent writes to the disk. Paused ones are left alone, also on release.
DOWNLOADING = frozenset({"downloading", "metaDL", "forcedMetaDL", "stalledDL", "queuedDL", "forcedDL", "checkingDL",
                         "allocating"})
BRAKE_MARGIN = 2 * GB  # downloads go on at the limit plus this, so the brake doesn't flap


def brake_decision(free: int | None, active: bool, limit: int) -> str | None:
    """'pull' below the limit, 'release' when the brake is on and there is the limit plus the margin, else None."""
    if free is None or limit <= 0:
        return None
    if free < limit:
        return "pull"
    if active and free >= limit + BRAKE_MARGIN:
        return "release"
    return None


def brake_pulled_text(free: int, names: list[str], limit: int) -> str:
    """names: the paused downloads, already escaped for Discord."""
    count = len(names)
    what = "1 Download" if count == 1 else f"{count} Downloads"
    shown = ", ".join(f"*{shorten(name, 60)}*" for name in names[:3]) + (f" und {count - 3} weitere" if count > 3 else "")
    return (f"🛑 **Notbremse:** Nur noch {size_text(free)} frei. Ich habe {what} in qBittorrent angehalten ({shown}), "
            f"damit die Platte des Servers nicht vollläuft. Sie laufen von selbst weiter, sobald wieder "
            f"{size_text(limit + BRAKE_MARGIN)} frei sind. Platz schaffen: `/speicher` und `/löschen`.")


def brake_released_text(free: int, count: int) -> str:
    if not count:
        return f"✅ **Notbremse gelöst:** Wieder {size_text(free)} frei."
    what = "1 Download läuft" if count == 1 else f"{count} Downloads laufen"
    return f"✅ **Notbremse gelöst:** Wieder {size_text(free)} frei, {what} weiter."


# --- /folgen ---------------------------------------------------------------------------------
WHOLE_SEASON = 9999
MAX_EPISODES = 200  # a typo like 1-1170 should not grab a whole anime
EPISODES_HELP = "Beispiele: `S23E18-E25`, `S23`, `S01E05`, bei Anime auch durchgehend `1155-1170`, mehrere mit Komma."
_DASH = re.compile(r"\s*(?:–|—|-|\bbis\b|\bto\b)\s*", re.IGNORECASE)
_SEASON = re.compile(r"^s(\d{1,3})(?:e(\d{1,4}))?$", re.IGNORECASE)
_EPISODE = re.compile(r"^e(\d{1,4})$", re.IGNORECASE)
_NUMBER = re.compile(r"^(\d{1,5})$")


@dataclass(frozen=True)
class Span:
    """Episodes from start to end, both included: (season, episode), or (None, number) for absolute numbers."""
    start: tuple[int | None, int]
    end: tuple[int | None, int]

    @property
    def absolute(self) -> bool:
        return self.start[0] is None


def parse_episodes(text: str) -> list[Span]:
    """'S23E18-E25, 1155-1170' → spans. Raises ValueError with a message for Discord."""
    spans = []
    for part in re.split(r"[,;]+", text):
        part = part.strip()
        if not part:
            continue
        pieces = [p.replace(" ", "") for p in _DASH.split(part)]
        if len(pieces) > 2 or not all(pieces):
            raise ValueError(f"Das verstehe ich nicht: „{part}“. {EPISODES_HELP}")
        first = _SEASON.match(pieces[0])
        if first:
            season = int(first.group(1))
            episode = int(first.group(2)) if first.group(2) else None
            start = (season, episode or 1)
            end: tuple[int | None, int] = (season, episode or WHOLE_SEASON)
            if len(pieces) == 2:
                to_season, to_episode = _SEASON.match(pieces[1]), _EPISODE.match(pieces[1]) or _NUMBER.match(pieces[1])
                if to_season:
                    end = (int(to_season.group(1)), int(to_season.group(2)) if to_season.group(2) else WHOLE_SEASON)
                elif to_episode and episode is not None:
                    end = (season, int(to_episode.group(1)))
                else:
                    raise ValueError(f"Das verstehe ich nicht: „{part}“. {EPISODES_HELP}")
        else:
            numbers = [_NUMBER.match(p) for p in pieces]
            if not all(numbers):
                raise ValueError(f"Das verstehe ich nicht: „{part}“. {EPISODES_HELP}")
            start = (None, int(numbers[0].group(1)))  # type: ignore[union-attr]
            end = (None, int(numbers[-1].group(1)))  # type: ignore[union-attr]
        if end[1] < start[1] if start[0] == end[0] else (end[0] or 0) < (start[0] or 0):
            raise ValueError(f"„{part}“ hört auf, bevor es anfängt.")
        spans.append(Span(start, end))
    if not spans:
        raise ValueError(f"Welche Folgen? {EPISODES_HELP}")
    return spans


def select_episodes(episodes: list[dict], spans: list[Span]) -> list[dict]:
    """The Sonarr episodes the spans mean, by season and episode. Specials only if season 0 is asked for."""
    if any(s.absolute for s in spans) and not any(isinstance(e.get("absoluteEpisodeNumber"), int) for e in episodes):
        raise ValueError("Diese Serie hat keine durchgehenden Folgennummern. Bitte mit Staffel, z.B. `S01E05-E10`.")
    chosen: dict[int, dict] = {}
    for e in episodes:
        season, number = e.get("seasonNumber"), e.get("episodeNumber")
        if not isinstance(season, int) or not isinstance(number, int) or not isinstance(e.get("id"), int):
            continue
        for span in spans:
            if span.absolute:
                n = e.get("absoluteEpisodeNumber")
                hit = season > 0 and isinstance(n, int) and span.start[1] <= n <= span.end[1]
            else:
                hit = span.start <= (season, number) <= span.end  # type: ignore[operator]
            if hit:
                chosen[e["id"]] = e
                break
    return sorted(chosen.values(), key=lambda e: (e["seasonNumber"], e["episodeNumber"]))


@dataclass(frozen=True)
class EpisodePlan:
    have: list[dict]  # already there
    get: list[dict]  # out, but missing: searched right away
    later: list[dict]  # not out yet: monitored, Sonarr takes them when they come out
    per_episode: int  # estimated bytes

    @property
    def size(self) -> int:
        return self.per_episode * len(self.get)


def plan_episodes(chosen: list[dict], now: datetime, per_episode: int) -> EpisodePlan:
    have = [e for e in chosen if e.get("hasFile")]
    out = [e for e in chosen if not e.get("hasFile") and (t := parse_time(e.get("airDateUtc"))) is not None and t <= now]
    later = [e for e in chosen if not e.get("hasFile") and e not in out]
    return EpisodePlan(have, out, later, per_episode)


def _runs(numbers: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def episodes_text(episodes: list[dict], limit: int = 6) -> str:
    """'S23E18–E25 (Folge 1173–1180)'; long lists are cut."""
    by_season: dict[int, list[int]] = {}
    for e in episodes:
        by_season.setdefault(e["seasonNumber"], []).append(e["episodeNumber"])
    parts = [f"S{season:02d}E{a:02d}" + (f"–E{b:02d}" if b != a else "")
             for season in sorted(by_season) for a, b in _runs(by_season[season])]
    text = ", ".join(parts[:limit]) + (f" und {len(parts) - limit} weitere" if len(parts) > limit else "")
    absolute = [e["absoluteEpisodeNumber"] for e in episodes if isinstance(e.get("absoluteEpisodeNumber"), int)]
    if absolute and len(absolute) == len(episodes) and len(_runs(absolute)) <= 3:
        text += " (Folge " + ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in _runs(absolute)) + ")"
    return text


def per_episode_estimate(series: dict, gb_per_hour: float) -> int:
    """Bytes per episode: the average of the files already there, else running time × GB per hour."""
    stats = series.get("statistics") if isinstance(series.get("statistics"), dict) else {}
    files, size = stats.get("episodeFileCount"), stats.get("sizeOnDisk")
    if isinstance(files, int) and files > 0 and isinstance(size, int) and size > 0:
        return size // files
    runtime = series.get("runtime") if isinstance(series.get("runtime"), int) and series["runtime"] > 0 else 24
    return int(runtime / 60 * gb_per_hour * GB)


def plan_lines(plan: EpisodePlan, free: int | None, minimum: int) -> list[str]:
    lines = []
    if plan.get:
        lines.append(f"🔎 **{len(plan.get)} {'Folge' if len(plan.get) == 1 else 'Folgen'} holen:** {episodes_text(plan.get)}"
                     f" · ≈ {size_text(plan.size)}")
    if plan.later:
        lines.append(f"📅 **Noch nicht erschienen:** {episodes_text(plan.later)}. Sonarr holt sie, sobald sie draussen sind.")
    if plan.have:
        lines.append(f"✅ **Schon da:** {episodes_text(plan.have)}")
    if free is not None and plan.get:
        after = free - plan.size
        line = f"💾 Frei: {size_text(free)}, danach ≈ {size_text(max(after, 0))}"
        if after < minimum:
            line += f". ⚠️ Das ist unter der Grenze von {size_text(minimum)}, die Notbremse kann die Downloads anhalten."
        lines.append(line)
    return lines


# --- /zufall -------------------------------------------------------------------------------------
def pick_candidates(items: list[dict], played: set[str], kind: str | None, genre: str | None) -> list[dict]:
    """Movies and shows from Jellyfin that weren't watched, optionally only one kind or genre."""
    types = {"movie": {"Movie"}, "tv": {"Series"}}.get(kind or "", {"Movie", "Series"})
    wanted = (genre or "").strip().casefold()
    return [i for i in items if i.get("Type") in types and str(i.get("Id")) not in played
            and (not wanted or any(wanted == str(g).casefold() for g in i.get("Genres") or []))]


def runtime_text(ticks: object) -> str:
    if not isinstance(ticks, int) or ticks <= 0:
        return ""
    minutes = round(ticks / 600_000_000)
    return f"{minutes // 60} Std. {minutes % 60:02d} Min." if minutes >= 60 else f"{minutes} Min."


def pick_facts(item: dict) -> str:
    parts = ["🎬 Film" if item.get("Type") == "Movie" else "📺 Serie"]
    if item.get("Type") == "Series" and isinstance(item.get("ChildCount"), int) and item["ChildCount"] > 0:
        parts[0] += f" · {item['ChildCount']} {'Staffel' if item['ChildCount'] == 1 else 'Staffeln'}"
    if item.get("Type") == "Movie" and runtime_text(item.get("RunTimeTicks")):
        parts.append(runtime_text(item.get("RunTimeTicks")))
    if isinstance(item.get("CommunityRating"), (int, float)):
        parts.append(f"⭐ {item['CommunityRating']:.1f}")
    genres = [str(g) for g in item.get("Genres") or []][:3]
    if genres:
        parts.append(", ".join(genres))
    return " · ".join(parts)


def item_label(item: dict) -> str:
    year = item.get("ProductionYear")
    return f"{item.get('Name', '?')} ({year})" if year else str(item.get("Name", "?"))


# --- /filmabend ----------------------------------------------------------------------------------
_WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
             "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WHEN = re.compile(r"^(?:(?P<word>[a-zäöü]+)\.?\s*|(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{2,4})?\s*)?"
                   r"(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?$")
WHEN_HELP = "Zum Beispiel `20:00`, `morgen 20:15`, `fr 20:00` oder `21.11. 20:00`."


def parse_when(text: str, now: datetime) -> datetime:
    """When the movie night starts, in the time zone of now. Just a time: today, or tomorrow if it passed."""
    cleaned = " ".join(text.strip().lower().replace("uhr", " ").split())
    match = _WHEN.match(cleaned)
    if not match:
        raise ValueError(f"Diese Zeit verstehe ich nicht: „{text.strip()}“. {WHEN_HELP}")
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"„{text.strip()}“ ist keine Uhrzeit. {WHEN_HELP}")
    day: date = now.date()
    word = match["word"]
    if word:
        if word == "heute":
            pass
        elif word == "morgen":
            day += timedelta(days=1)
        elif word == "übermorgen":
            day += timedelta(days=2)
        elif word[:3] in _WEEKDAYS or word[:2] in _WEEKDAYS:
            weekday = _WEEKDAYS.get(word[:3], _WEEKDAYS.get(word[:2]))
            day += timedelta(days=(weekday - now.weekday()) % 7)  # type: ignore[operator]
        else:
            raise ValueError(f"Diesen Tag verstehe ich nicht: „{word}“. {WHEN_HELP}")
    elif match["day"]:
        year = int(match["year"]) if match["year"] else now.year
        year += 2000 if year < 100 else 0
        try:
            day = date(year, int(match["month"]), int(match["day"]))
        except ValueError as exc:
            raise ValueError(f"Dieses Datum gibt es nicht: „{text.strip()}“.") from exc
    when = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
    if when <= now:
        if not word and not match["day"]:
            when += timedelta(days=1)  # "20:00" late in the evening means tomorrow
        elif word and word not in ("heute", "morgen", "übermorgen"):
            when += timedelta(days=7)  # "fr 20:00" on a Friday night means next Friday
        elif match["day"] and not match["year"]:
            when = when.replace(year=when.year + 1)
        else:
            raise ValueError("Diese Zeit ist schon vorbei.")
    if when - now > timedelta(days=365):
        raise ValueError("Das ist mehr als ein Jahr weg.")
    return when


def event_description(label: str, link: str | None) -> str:
    """For the Discord event: what is watched and how SyncPlay works (Discord allows 1000 characters)."""
    text = (f"Wir schauen zusammen {label} in Jellyfin.\n\n"
            "So geht's mit SyncPlay:\n"
            "1. Jellyfin öffnen und anmelden.\n"
            "2. Oben auf das SyncPlay-Symbol (zwei Personen) tippen.\n"
            "3. Wer zuerst da ist: „Neue Gruppe“. Alle anderen treten dieser Gruppe bei.\n"
            "4. Film starten: Alle sehen ihn gleichzeitig, Pause und Spulen gelten für alle.")
    if link:
        text += f"\n\n{link}"
    return text[:1000]


def event_end(start: datetime, ticks: object) -> datetime:
    """Discord needs an end for events outside a voice channel: running time plus a quarter hour, at least 2 hours."""
    minutes = round(ticks / 600_000_000) if isinstance(ticks, int) and ticks > 0 else 0
    return start + timedelta(minutes=max(minutes + 15, 120))


# --- after watching: "Geschaut, löschen?" ----------------------------------------------------------
def _size(item: dict) -> int:
    sources = item.get("MediaSources") if isinstance(item.get("MediaSources"), list) else []
    return sum(s.get("Size") or 0 for s in sources[:1] if isinstance(s, dict) and isinstance(s.get("Size"), int))


def watched_questions(played: dict[str, tuple[str, list[dict]]], since: datetime, asked: set[str],
                      series_tmdb: dict[str, int | None]) -> list[WatchQuestion]:
    """What was watched to the end after since and not asked about yet: one question per movie, and one per show
    for all its episodes. played: Jellyfin user id → (user name, recently played items)."""
    groups: dict[str, WatchQuestion] = {}
    for _, (user_name, items) in played.items():
        for item in items:
            data = item.get("UserData") if isinstance(item.get("UserData"), dict) else {}
            when = parse_time(data.get("LastPlayedDate"))
            item_id = str(item.get("Id") or "")
            if not data.get("Played") or when is None or when <= since or not item_id or item_id in asked:
                continue
            if item.get("Type") == "Movie":
                tmdb = str((item.get("ProviderIds") or {}).get("Tmdb") or "")
                question = groups.setdefault(f"movie:{item_id}", WatchQuestion(
                    "movie", int(tmdb) if tmdb.isdigit() else None, item_label(item)))
            elif item.get("Type") == "Episode" and item.get("SeriesId"):
                series_id = str(item["SeriesId"])
                question = groups.setdefault(f"tv:{series_id}", WatchQuestion(
                    "tv", series_tmdb.get(series_id), str(item.get("SeriesName") or "?"), series_id))
                key = [item.get("ParentIndexNumber"), item.get("IndexNumber")]
                if all(isinstance(n, int) for n in key) and key not in question.episodes:
                    question.episodes.append(key)
            else:
                continue
            if item_id not in question.item_ids:
                question.item_ids.append(item_id)
                question.size += _size(item)
            if user_name not in question.viewers:
                question.viewers.append(user_name)
    for question in groups.values():
        question.episodes.sort()
    return list(groups.values())


def watched_text(question: WatchQuestion, others: list[str]) -> str:
    """The question; names already escaped for Discord."""
    what = f"**{question.label}**"
    if question.kind == "tv" and question.episodes:
        what += " · " + episodes_text([{"seasonNumber": s, "episodeNumber": e} for s, e in question.episodes])
    lines = [f"👀 **Geschaut:** {what}" + (f" · {size_text(question.size)}" if question.size else ""),
             f"Von: {', '.join(question.viewers)}"]
    if others:
        lines.append(f"👥 Noch nicht fertig geschaut von: {', '.join(others)}")
    if question.kind == "tv":
        lines.append("Löschen, damit Platz frei wird? Sonarr holt diese Folgen danach nicht wieder.")
    else:
        lines.append("Löschen, damit Platz frei wird? Mit `/film` kann man ihn später wieder anfragen.")
    return "\n".join(lines)


def episodes_deleted_text(count: int, freed: int, removed: tuple[int, int, int] | None, problem: str | None) -> str:
    """After deleting watched episodes. removed: (torrents removed, their size, torrents still busy) or None."""
    what = "1 Folge" if count == 1 else f"{count} Folgen"
    text = f"🗑️ {what} gelöscht" + (f" (≈ {size_text(freed)})" if freed else "") + ", Sonarr holt sie nicht wieder."
    if problem:
        return text + f" ⚠️ Die Torrents sind noch in qBittorrent: {problem}"
    if removed is None:
        return text + " Der Platz wird erst frei, wenn auch der Torrent in qBittorrent weg ist."
    taken, _, busy = removed
    if taken:
        text += " 1 Torrent entfernt." if taken == 1 else f" {taken} Torrents entfernt."
    if busy:
        text += " Ein Torrent lädt noch oder wird gerade übernommen, sein Platz wird später frei."
    return text


# --- /folgen picker: seasons and episodes to click ----------------------------------------------------------
PICKER_PAGE = 75  # three menus of 25 episodes per page


def season_numbers(episodes: list[dict]) -> list[int]:
    """The seasons with episodes, specials (0) last."""
    seasons = sorted({e["seasonNumber"] for e in episodes if isinstance(e.get("seasonNumber"), int)})
    return [s for s in seasons if s > 0] + ([0] if 0 in seasons else [])


def season_episodes(episodes: list[dict], season: int) -> list[dict]:
    return sorted((e for e in episodes if e.get("seasonNumber") == season and isinstance(e.get("id"), int)),
                  key=lambda e: e.get("episodeNumber") or 0)


def first_season_to_pick(episodes: list[dict]) -> int:
    """The latest season that still has missing episodes, else the latest season."""
    seasons = [s for s in season_numbers(episodes) if s > 0] or season_numbers(episodes) or [1]
    missing = [s for s in seasons if any(not e.get("hasFile") for e in season_episodes(episodes, s))]
    return (missing or seasons)[-1]


def episode_label(e: dict, anime: bool) -> str:
    number = f"E{e.get('episodeNumber', 0):02d}"
    if anime and isinstance(e.get("absoluteEpisodeNumber"), int):
        number += f" · Folge {e['absoluteEpisodeNumber']}"
    title = str(e.get("title") or "").strip()
    return shorten(f"{number} · {title}" if title and title.upper() != "TBA" else number, 100)


def episode_state(e: dict, loading: set[int], now: datetime, zone) -> str:
    """What the menu shows below an episode."""
    if e.get("hasFile"):
        return "✅ schon da"
    if e.get("id") in loading:
        return "⬇️ lädt gerade"
    when = parse_time(e.get("airDateUtc"))
    if when is None:
        return "📅 Termin noch offen"
    if when > now:
        return f"📅 erscheint {when.astimezone(zone):%d.%m.%Y}"
    return f"🔎 erschienen {when.astimezone(zone):%d.%m.%Y}" + (", wird schon gesucht" if e.get("monitored") else "")


def picker_lines(episodes: list[dict], selected: set[int], loading: set[int], now: datetime, zone,
                 anime: bool) -> list[str]:
    """The episodes of one page in the embed: there, loading, chosen, not chosen, still to come."""
    lines = []
    for e in episodes:
        if e.get("hasFile"):
            mark = "✅"
        elif e.get("id") in loading:
            mark = "⬇️"
        elif e.get("id") in selected:
            mark = "☑️"
        else:
            when = parse_time(e.get("airDateUtc"))
            mark = "📅" if when is None or when > now else "⬜"
        lines.append(f"{mark} {episode_label(e, anime)}")
    return lines


# --- early search ---------------------------------------------------------------------------------------
def early_search_ids(episodes: list[dict], loading: set[int], now: datetime, ahead_days: int,
                     back_days: int = 2) -> list[int]:
    """Monitored episodes without a file that come out within ahead_days or came out in the last back_days and aren't
    downloading. Streaming services often publish before the TV date TVDB has (Dexter: Friday, TVDB: Monday), and
    Sonarr's RSS check can miss a release, so these are searched directly."""
    ids = []
    for e in episodes:
        when = parse_time(e.get("airDateUtc"))
        if (when is None or not isinstance(e.get("id"), int) or e.get("hasFile") or not e.get("monitored", True)
                or e["id"] in loading or (e.get("seasonNumber") or 0) <= 0):
            continue
        if now - timedelta(days=back_days) <= when <= now + timedelta(days=ahead_days):
            ids.append(e["id"])
    return sorted(ids)


# --- /verfolgen and /kalender ---------------------------------------------------------------------------
WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def when_text(when: datetime, zone) -> str:
    local = when.astimezone(zone)
    return f"{WEEKDAYS_SHORT[local.weekday()]} {local:%d.%m.} um {local:%H:%M}"


def parse_start(text: str | None) -> Span | None:
    """/verfolgen ab: one episode ("S02E04", or absolute "1181" for anime); empty or "jetzt": only what is to come."""
    cleaned = (text or "").strip()
    if not cleaned or cleaned.lower() in ("jetzt", "neu", "now"):
        return None
    spans = parse_episodes(cleaned)
    if len(spans) != 1 or spans[0].start != spans[0].end and not (spans[0].start[0] is not None and spans[0].end[1] == WHOLE_SEASON):
        raise ValueError("Bei `ab` bitte genau eine Folge angeben, z.B. `S02E04` oder bei Anime `1181`.")
    return spans[0]


def start_episode(span: Span | None, episodes: list[dict]) -> tuple[int, int] | None:
    """The (season, episode) to start at; absolute numbers are looked up in Sonarr's episodes."""
    if span is None:
        return None
    if not span.absolute:
        return span.start  # type: ignore[return-value]
    match = next((e for e in episodes if e.get("absoluteEpisodeNumber") == span.start[1]
                  and isinstance(e.get("seasonNumber"), int) and e["seasonNumber"] > 0), None)
    if match is None:
        raise ValueError(f"Folge {span.start[1]} gibt es bei dieser Serie nicht (oder sie hat keine durchgehende Nummerierung). "
                         "Bitte mit Staffel, z.B. `S02E04`.")
    return match["seasonNumber"], match["episodeNumber"]


def follow_lines(label: str, start: tuple[int, int] | None, get: list[dict], later: list[dict], have: list[dict],
                 new_seasons: bool, zone) -> list[str]:
    since = f"ab S{start[0]:02d}E{start[1]:02d}" if start else "ab jetzt (nur was noch kommt)"
    lines = [f"📺 **{label}** wird verfolgt, {since}."]
    if get:
        lines.append(f"🔎 **Schon erschienen, wird jetzt geholt:** {episodes_text(get)}")
    dated = sorted((e for e in later if parse_time(e.get("airDateUtc"))), key=lambda e: e["airDateUtc"])
    if dated:
        first, last = dated[0], dated[-1]
        line = (f"📅 **Nächste Folge: {when_text(parse_time(first['airDateUtc']), zone)}** "  # type: ignore[arg-type]
                f"(S{first['seasonNumber']:02d}E{first['episodeNumber']:02d})")
        if len(dated) > 1:
            line += f", danach {len(dated) - 1} weitere bis {when_text(parse_time(last['airDateUtc']), zone)[:9]}"  # type: ignore[arg-type]
        lines.append(line.rstrip(".") + ". Jede kommt von selbst, sobald sie draussen ist.")
    elif later:
        lines.append(f"📅 {len(later)} angekündigte Folgen ohne Datum, Sonarr holt sie, sobald sie erscheinen.")
    elif not get:
        lines.append("📭 Im Moment ist keine neue Folge angekündigt." if not have else "✅ Alles ab dort ist schon da.")
    if have and (get or later):
        lines.append(f"✅ Schon da: {episodes_text(have)}")
    lines.append("🆕 Neue Staffeln werden auch geholt." if new_seasons else "Neue Staffeln werden nicht automatisch geholt.")
    return lines


def calendar_lines(episodes: list[dict], movies: list[dict], loading: set[int], loading_movies: set[int],
                   start: datetime, end: datetime, now: datetime, zone) -> list[str]:
    """/kalender, by day: monitored episodes with time and state, movies on their digital release day."""
    entries: list[tuple[datetime, bool, str]] = []  # (when, has a time, line)
    for e in episodes:
        when = parse_time(e.get("airDateUtc"))
        show = e.get("series") if isinstance(e.get("series"), dict) else {}
        if when is None or not start <= when < end or not isinstance(e.get("seasonNumber"), int):
            continue
        what = f"S{e['seasonNumber']:02d}E{e.get('episodeNumber', 0):02d}"
        if isinstance(e.get("absoluteEpisodeNumber"), int) and show.get("seriesType") == "anime":
            what += f" (Folge {e['absoluteEpisodeNumber']})"
        if e.get("hasFile"):
            state = "✅ da"
        elif e.get("id") in loading:
            state = "⬇️ lädt"
        elif when <= now:
            state = "🔎 wird gesucht"
        else:
            state = "⏳ wird geholt"
        entries.append((when, True, f"📺 {show.get('title', '?')} · {what} · {state}"))
    for m in movies:
        when = parse_time(m.get("digitalRelease"))
        if when is None or not start <= when < end:
            continue
        state = "✅ da" if m.get("hasFile") else "⬇️ lädt" if m.get("id") in loading_movies else "⏳ wird geholt"
        year = f" ({m['year']})" if m.get("year") else ""
        entries.append((when, False, f"🎬 {m.get('title', '?')}{year} · erscheint digital · {state}"))
    lines: list[str] = []
    day = None
    for when, timed, line in sorted(entries, key=lambda x: x[0]):
        local = when.astimezone(zone)
        if local.date() != day:
            if day is not None:
                lines.append("")  # empty line between days
            day = local.date()
            lines.append(f"**{WEEKDAYS_SHORT[local.weekday()]} {local:%d.%m.}**")
        lines.append((f"`{local:%H:%M}` " if timed else "") + line)
    return lines
