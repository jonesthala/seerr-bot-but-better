# seerr bot but better

Request movies and shows from Discord, and a bit more. The bot searches [Seerr](https://docs.seerr.dev), shows
the poster and whether a title is already there, and requests it with one click; Seerr hands it to Radarr or
Sonarr as usual. With Sonarr, Radarr, qBittorrent and Jellyfin connected (all optional) it also shows live
download progress, follows shows episode by episode, has a calendar, cleans up after watching and keeps the
disk from running full.

```text
/film Sint…        (suggestions while you type: Sintel (2010) · Film · ➕ Noch nicht da …)
  Sintel (2010) · ➕ Noch nicht da, kann angefragt werden      [📥 Anfragen] [Abbrechen]
  ⏳ Angefragt! Der Film wartet noch auf die Freigabe durch einen Admin. Ich sage dir hier Bescheid, sobald alles da ist.

  📥 Neue Anfrage von alex: Sintel (2010). Wartet auf Freigabe.      [✅ Freigeben] [✖️ Ablehnen]

/status
  📺 One Piece (1999)   🟦🟦🟦🟦⬜⬜⬜⬜⬜⬜  42 %   7.3 von 17.5 GB · 4.3 MB/s · fertig noch ca. 40 Min.
  Lädt gerade: S23E01 🟦🟦🟦🟦🟦⬜⬜⬜ 63 % …   Pausiert: S23 Folge 13–17

  🍿 @alex Sintel (2010) ist jetzt da. Viel Spass!      [▶️ Jetzt ansehen]

/verfolgen Dexter ab S02E04
  📺 Dexter: Resurrection (2025) wird verfolgt, ab S02E04.
  📅 Nächste Folge: Mo 23.11. um 02:00 (S02E04), danach 6 weitere bis Mo 04.01.

  (DM) 👀 Geschaut: Dune (2021) · 4.9 GB. Löschen, damit Platz frei wird?      [🗑️ Löschen] [👍 Behalten]
```

> The bot's messages are in German.

## Features

**Requesting**
- **`/film <titel>` and `/serie <titel>`**: up to five hits to pick from, with poster, year, short
  description and status (already there, partly there, requested, downloading). Discord **suggests
  titles while you type**; picking one opens it directly.
- **Seasons.** For shows, the missing seasons are preselected; you can narrow them down. Specials are left out.
- **Quality or language** (optional): offer some of your Radarr/Sonarr quality profiles, e.g. 1080p and 4K,
  or a German and an original-language profile.
- **`/trending`**: what is popular right now, ready to request.
- **Size estimate before requesting**: the request window shows roughly how many GB the chosen movie or
  seasons will take (running time from TMDB × GB per hour), the free space, and a warning if it gets tight.
  With the Jellyfin setup below, GB per hour is measured in your own library (hourly); otherwise a rule of
  thumb (1.8 GB per hour). It updates when you change the seasons.
- **Limit per person** (optional), e.g. 3 requests per week. Admins have no limit.

**Following up**
- **Approval in Discord.** Requests that wait for approval (also those made in Seerr itself) get
  *Freigeben* / *Ablehnen* buttons that only admins can use. Requests handled in Seerr update the message.
- **"It's there."** The bot remembers who requested what and mentions them when it is in the library,
  with a *Jetzt ansehen* button to Jellyfin. Also when a request was declined.
- **`/status`**: one card per title with poster and an overall progress bar: requests waiting for approval,
  what is being searched or downloaded, who asked for it, and free space. With Sonarr/Radarr (setup step 5): episodes there,
  downloading, missing (which ones) and still to come, each download with a progress bar, time left and, with
  qBittorrent, speed and sources; stuck downloads with the reason; whether a search is running. A button
  refreshes it. Who requests a show gets an automatic `/abo` for it, so new episodes arrive as DM. Downloads
  without a request in Seerr (from `/folgen` or added in Sonarr/Radarr) get a card too.
- **`/neu`**: what came into the library last, with links, shows that are only partly there marked as such
  (e.g. „teilweise da (Staffel 23)“). Optionally as a **weekly post** in the channel.
- **`/problem <titel>`**: report a problem (picture, sound, subtitles, other) as an issue in Seerr.
- **`/hilfe`**: the commands this person may use, each with an example. Admin commands only for admins.
- **`/abo <serie>`** (needs the Jellyfin setup below): subscribe to a show in the library and get a DM when new
  episodes arrive in Jellyfin, grouped by season. Choosing the show again ends it; without a show it lists
  your subscriptions. Episodes already there when subscribing are not announced.
- **`/verfolgen <serie> [ab] [neue_staffeln] [stopp]`** (needs Sonarr): follow a show from an episode on, e.g.
  `/verfolgen Dexter ab S02E04` (or `1181` for anime; without `ab` only what is still to come). Shows that aren't
  in Sonarr yet are added without their old episodes, with Seerr's quality profile and folder. Every new episode is
  fetched when it comes out, optionally new seasons too, and the follower gets an automatic `/abo`. The answer says
  when the next episode comes. Admins follow directly; for everyone else an admin approves it with buttons. Without
  a show it lists what you follow; `stopp:True` stops fetching what is still to come (files stay).
- **`/kalender [tage] [mein]`** (needs Sonarr): the coming days, by day: monitored episodes with time and state
  (fetched, downloading, there) and movies on their digital release; `mein` only shows you follow or subscribed to.
- **`/zufall [Filme|Serien] [genre]`** (needs the Jellyfin setup): a movie or show from the library that wasn't
  watched yet (by you if you have a Jellyfin account through the bot, else by anyone), with a *Nochmal* button.
- **`/filmabend <titel> <zeit>`** (needs the Jellyfin setup): creates a Discord event (`20:00`, `morgen 20:15`,
  `fr 20:00`, `21.11. 20:00`) with the poster and a SyncPlay guide; Discord reminds everyone who clicked
  *Interessiert*. The bot needs the *Create Events* permission.

**Housekeeping**
- **Watches the disk.** Below `MIN_FREE_GB` free, new requests are refused with a clear message, and the
  bot posts once in the channel when space gets low and once when it is fine again.
- **Download brake** (with qBittorrent, setup step 5): below `BRAKE_FREE_GB` free, the bot pauses the running
  downloads in qBittorrent and says so in the channel; once there is 2 GB more again, it starts exactly those
  again. Requests are refused earlier, but downloads that already run would otherwise fill the disk.
- **Early search** (needs Sonarr): every hour the bot has Sonarr search monitored episodes that come out within
  `EARLY_SEARCH_DAYS` (default 4) or came out in the last two days and are still missing. Streaming services often
  publish days before the TV date TheTVDB has (e.g. Friday instead of Monday), and Sonarr's RSS check alone can miss
  a release on busy indexers.
- **"Geschaut, löschen?"** (needs the Jellyfin setup and admins): every few minutes the bot looks which movies
  and episodes someone watched to the end in Jellyfin and asks the admins by DM, with *Löschen* / *Behalten*
  buttons, who watched it and who of the others hasn't finished it yet. Episodes of one show come as one
  question. *Löschen* deletes a movie like `/löschen`; for episodes it deletes just their files in Sonarr, stops
  monitoring them (so they aren't fetched again) and removes their finished torrents. Only what is watched after
  the bot starts counts. `WATCHED_ASK=0` turns it off.
- **`/folgen <serie> <folgen>`** (admins only, needs Sonarr): single episodes instead of whole seasons, e.g.
  `S23E18-E25`, `S23` or, for anime, `1155-1170`. Shows what is there, what is missing and how much space it
  needs; *Holen* monitors exactly these episodes in Sonarr and searches them.
- **`/löschen <titel>`** (admins only): pick a movie or show from the library and delete it, files
  included, after a second confirmation click. It is reset in Seerr, so it can be requested again later.
  With qBittorrent access (setup step 5) it also removes the title's torrents with their files, so the space
  is really freed, and says how much. Afterwards Jellyfin reads its library again and, three minutes after the last
  deletion, Seerr compares itself with it, so the title doesn't stay "available" in Seerr and `/neu`. Titles that
  Sonarr/Radarr don't know (files that were in the library before) are not deleted: Seerr would say "deleted" and
  leave the files, so the bot says so and names the folder to delete by hand.
- **Only where it belongs.** The bot answers in one channel; optionally only members with a role may request.
- **Your search, your buttons.** Only the person who searched can pick and request.

**Jellyfin accounts for friends** (optional, see [setup](#4-jellyfin-accounts-for-friends-optional))
- **`/jellyfin-zugang`**: a short form asks for a user name; admins get *Freigeben* / *Ablehnen* buttons.
- After approval the bot creates the Jellyfin account (only the libraries you name, no admin rights, nothing
  to delete, a bitrate limit for remote streaming, hidden on the login page) and sends the friend a
  *Zugangsdaten anzeigen* button, by DM or in the channel.
- The button shows the whole guide, the Tailscale share link and a **fresh password that only this person
  sees**. Passwords are never stored; a new click makes a new one, so it also works as "forgot password".
- **`/jellyfin-liste`** and **`/jellyfin-entziehen <person>`** (admins only): who has access, and block an account.
- **`/speicher`** (admins only): the ten titles that take the most space, how many files, when anyone last
  watched them and who requested them, plus free space. Helps to decide what `/löschen` should remove.

Seasons and quality are chosen per request. Seerr only requests whole seasons; for single episodes, admins
use `/folgen`, which talks to Sonarr directly.

## How it works

```mermaid
flowchart LR
    D[Discord channel and DMs] -- /film, /serie, /status … --> B[Bot]
    B -- REST API + API key --> S[Seerr]
    S --> R[Radarr / Sonarr]
    B -. optional: details, /folgen, /verfolgen, /kalender .-> R
    B -. optional: torrents, download brake .-> Q[qBittorrent]
    B -. optional: accounts, /zufall, watched .-> J[Jellyfin]
    B -. state.json .- V[(data volume)]
```

The bot talks to Discord and Seerr's API, and to Sonarr, Radarr, qBittorrent and Jellyfin if you set them up.
It looks on its own: every two minutes at Seerr for new requests to approve and finished downloads to announce,
every minute at the free space (download brake), every five minutes at what was watched in Jellyfin, every ten
minutes for new episodes of subscribed shows. It keeps one small file, `state.json` in `DATA_DIR` (`/data` in
Docker): who requested what through Discord, the approval messages it posted, the week of the last weekly
post, who asked for a Jellyfin account, the subscriptions and followed shows, the open "Geschaut, löschen?"
questions and which torrents the download brake paused. Without it the bot still works, but forgets that on a
restart.

## Setup

### 1. Create the Discord bot

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** → **Reset Token**, copy it.
2. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; bot permissions
   **View Channels**, **Send Messages**, **Embed Links**, and **Create Events** for `/filmabend`. Open the link
   and add the bot to your server. No privileged intents are needed. Admins listed in `DISCORD_ADMIN_USER_IDS`
   get DMs from the bot ("Geschaut, löschen?"), so they should allow DMs from server members.
3. Discord → User Settings → Advanced → **Developer Mode** on. Right-click → **Copy ID** for the server,
   the channel and (optionally) the role that may request.

### 2. Seerr API key

Seerr → **Settings → General → API Key**. The key has admin rights, keep it private.

Optional: create a Seerr user for Discord requests (Settings → Users) with the *Request* permission but
**without** "Auto-Approve", and put its ID into `SEERR_USER_ID`. The bot then makes requests *as* that user
(Seerr's `X-API-User` header), so its permissions apply and requests wait for approval: in Seerr, or with
the buttons in Discord once `DISCORD_ADMIN_USER_IDS` or `DISCORD_ADMIN_ROLE_ID` is set. Without
`SEERR_USER_ID`, requests are made by the API key's owner, an admin, and start at once.

If you offer quality profiles (`MOVIE_PROFILES`, `TV_PROFILES`) and Seerr ignores the choice, give that
user the *Advanced Requests* permission in Seerr.

### 3. Run it

```bash
git clone https://github.com/jonesthala/seerr-bot-but-better.git seerrbot && cd seerrbot
cp .env.example .env && chmod 600 .env    # fill in the values
docker compose up -d --build
```

Without Docker Compose:

```bash
docker build -t seerrbot .
docker run -d --name seerrbot --restart unless-stopped --env-file .env -v seerrbot-data:/data seerrbot
```

The volume `seerrbot-data` keeps `state.json` (who requested what) across updates.

Check that it started: `docker logs seerrbot` should end with `Logged in as <your bot>`. Then try
`/film Sintel` in your channel.

To update: `git pull`, then the same build and run again (remove the old container first with
`docker rm -f seerrbot` when not using Compose).

### 4. Jellyfin accounts for friends (optional)

For friends who reach Jellyfin through a [Tailscale share](https://tailscale.com/kb/1084/sharing) of the
Jellyfin machine. Needs admins (`DISCORD_ADMIN_USER_IDS` or `DISCORD_ADMIN_ROLE_ID`), because nobody
gets an account without an approval.

1. Jellyfin → **Dashboard → API Keys → +**. The key has admin rights: `JELLYFIN_API_KEY`.
2. Tailscale admin console → **Machines** → your Jellyfin machine → **Share** → **Copy invite link** → turn on
   **Reusable link** → **Copy share link**: `TAILSCALE_SHARE_URL`. The bot never gets a Tailscale key; anyone
   with this link only sees the Jellyfin machine and still needs an account. If it leaks, revoke it there and
   put a new one into `.env`; friends who lose access get the new link with their *Zugangsdaten anzeigen* button.
3. `JELLYFIN_API_URL`: how the bot reaches Jellyfin (e.g. `http://192.168.1.30:8096`).
   `JELLYFIN_FRIEND_URL`: the address friends type into the app (the machine's Tailscale IP with port 8096).
4. Optional: `JELLYFIN_FRIEND_LIBRARIES` (e.g. `Movies,Shows`) and `JELLYFIN_FRIEND_BITRATE_MBIT` (default 4).

The three commands appear once all four required values are set. Taking access away blocks the Jellyfin
account, so the person can't watch anything any more. The Tailscale share stays until you remove it in
Tailscale (Machines → Jellyfin → Share); the bot has no Tailscale access.

### 5. Sonarr, Radarr and qBittorrent (optional)

Seerr alone is enough for requesting. With direct access to Sonarr and Radarr (API keys from *Settings →
General*) and qBittorrent (WebUI user and password; qBittorrent 4.x has no API keys) the bot also:

- shows per title in `/status` which episodes are there, downloading or missing, with speed and time left;
- `/folgen`, `/verfolgen` and `/kalender` (Sonarr);
- removes a deleted title's torrents too, so the space is really freed (with hardlinks, a deleted title keeps
  taking space while its torrent is still seeding), and deletes watched episodes ("Geschaut, löschen?");
- pauses running downloads when the disk is nearly full (download brake, qBittorrent).

```env
QBITTORRENT_URL=http://192.168.1.20:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=...
SONARR_URL=http://192.168.1.20:8989
SONARR_API_KEY=...
RADARR_URL=http://192.168.1.20:7878
RADARR_API_KEY=...
```

Only torrents listed in the history of that title are removed, nothing else. If Sonarr/Radarr or qBittorrent
can't be asked, nothing is deleted. Files you added by hand (not through Sonarr/Radarr) stay in qBittorrent.
Each part works on its own: without qBittorrent there is no brake and torrents stay, without Radarr `/status`
shows movies as Seerr sees them.

Without Docker (Python 3.11 or newer):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a && . ./.env && set +a
python -m bot    # remembers its state in ./data unless DATA_DIR says otherwise
```

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | – | Bot token |
| `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` | – | Server and channel the bot works in |
| `DISCORD_REQUEST_ROLE_ID` | empty | Only this role may search and request; empty = everyone in the channel |
| `DISCORD_ADMIN_ROLE_ID`, `DISCORD_ADMIN_USER_IDS` | empty | Admins: `/löschen`, `/folgen`, `/speicher`, Jellyfin accounts, approving requests and `/verfolgen` of others; admins by user ID also get the "Geschaut, löschen?" DMs. Both empty = all of that off |
| `DISCORD_APPROVAL_CHANNEL_ID` | the bot's channel | Where requests waiting for approval are posted with buttons |
| `MIN_FREE_GB` | `15` | Refuse new requests and warn below this much free space |
| `BRAKE_FREE_GB` | `3` | Pause running downloads in qBittorrent below this much free space; `0` = off |
| `EARLY_SEARCH_DAYS` | `4` | Search monitored episodes this many days before their TV date (hourly); `0` = off |
| `WATCHED_ASK` | `1` | Ask the admins by DM whether to delete what was watched to the end in Jellyfin; `0` = off |
| `REQUEST_LIMIT`, `REQUEST_LIMIT_DAYS` | `0`, `7` | At most this many requests per person in this many days; `0` = no limit. Admins have none |
| `MOVIE_PROFILES`, `TV_PROFILES` | empty | Radarr/Sonarr quality profiles to choose from, by name, e.g. `HD-1080p,Ultra-HD`; the first is preselected |
| `WEEKLY_DIGEST` | empty | Post what's new every week, e.g. `sun 18:00` or `so 18:00`; empty = off |
| `TIMEZONE` | `UTC` | For the weekly post and dates, e.g. `Europe/Zurich` |
| `JELLYFIN_URL` | empty | Jellyfin's address for the *Jetzt ansehen* buttons; empty = the one Seerr uses |
| `JELLYFIN_API_URL`, `JELLYFIN_API_KEY` | empty | `/jellyfin-zugang`: how the bot reaches Jellyfin, and an API key from Jellyfin's dashboard |
| `JELLYFIN_FRIEND_URL` | empty | `/jellyfin-zugang`: the server address friends enter in the Jellyfin app |
| `TAILSCALE_SHARE_URL` | empty | `/jellyfin-zugang`: multi-use share link for the Jellyfin machine |
| `JELLYFIN_FRIEND_LIBRARIES` | empty = all | Libraries friends may see, by name, e.g. `Movies,Shows` |
| `JELLYFIN_FRIEND_BITRATE_MBIT` | `4` | Bitrate limit for friends streaming from outside; `0` = none |
| `QBITTORRENT_URL`, `QBITTORRENT_USERNAME`, `QBITTORRENT_PASSWORD` | empty | Speed in `/status`, removing torrents when deleting, the download brake |
| `SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`, `RADARR_API_KEY` | empty | Details in `/status`, which torrents belong to a title; Sonarr also for `/folgen`, `/verfolgen`, `/kalender` and deleting watched episodes |
| `DATA_DIR` | `data` (`/data` in Docker) | Where `state.json` is kept |
| `SEERR_URL` | `http://localhost:5055` | How the bot reaches Seerr |
| `SEERR_API_KEY` | – | Seerr API key |
| `SEERR_LANGUAGE` | `de` | Language of titles and descriptions |
| `SEERR_USER_ID` | empty | Make requests as this Seerr user; its permissions decide whether they wait for approval |
| `SEERR_PUBLIC_URL` | empty | Link to Seerr in the bot's messages |

## Security

- The Seerr API key can do everything an admin can. Keep `.env` private (`chmod 600`) and out of Git.
- Requests made as the admin are approved at once. If others can use the bot, set `SEERR_USER_ID` or a
  request role.
- The container runs as an unprivileged user and needs no ports.
- `/löschen` removes files for good. Only the admins you list can use it, and it always asks twice.
- Approve and decline buttons check on every click that the person is an admin.
- The bot only ever mentions the person who made a request, never `@everyone` or roles.
- `state.json` holds Discord user IDs and display names of people who requested something, and for Jellyfin
  accounts the user name, the account ID and the state. Never a password.
- With `QBITTORRENT_*` the bot can remove torrents and their files. It only ever removes those Sonarr/Radarr list
  for what an admin deletes. The download brake only pauses running downloads and later starts exactly those again.
- What the bot changes in Sonarr: monitoring and searching episodes (`/folgen`, `/verfolgen`), adding a show
  without its old episodes (`/verfolgen`), deleting the files of watched episodes after an admin clicks *Löschen*.
  `/folgen` is for admins only; `/verfolgen` of others needs an admin's approval. Radarr is only changed through
  Seerr (`/löschen`).
- In Jellyfin the bot also starts a library scan after deleting; in Seerr its availability sync.
- The Jellyfin API key can do everything an admin can. Friends' accounts are only created after an admin's
  click, and a password is only shown to the person whose button it is.

## Deleting and hardlinks

Radarr and Sonarr usually import downloads as **hardlinks**: the file in the library and the one your
torrent client is still seeding share the same disk space. Deleting from the library alone frees
**nothing** while the torrent is still there. Give your torrent client a seeding limit (ratio or time)
that removes finished torrents, or remove them by hand, so `/löschen` really frees space.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `Improper token has been passed`, or the log says `DISCORD_TOKEN is the app's Public Key` | You copied the Public Key or the Client Secret. The token is only on the app's **Bot** page: **Reset Token**. |
| `Environment variable … is missing` or `… must be a number` | A value in `.env` is empty or has a typo. IDs are numbers only (Developer Mode → Copy ID). |
| The slash commands don't show up | The bot registers them for `DISCORD_GUILD_ID` when it starts. Check the server ID and that the invite link had the `applications.commands` scope, then reload Discord (Ctrl+R). |
| „Bitte im #kanal benutzen.“ | The bot only answers in `DISCORD_CHANNEL_ID`. |
| „Seerr lehnt den API-Key ab“ | Wrong `SEERR_API_KEY`, or it was regenerated in Seerr. |
| Requests are refused because of space | Less than `MIN_FREE_GB` is free in Radarr's or Sonarr's root folder. Free some space (see [Deleting and hardlinks](#deleting-and-hardlinks)) or lower the value. |
| `/löschen` says deleting is off, or no approval buttons appear | Set `DISCORD_ADMIN_USER_IDS` or `DISCORD_ADMIN_ROLE_ID`. Buttons only appear for requests that really wait for approval (not for auto-approved ones). |
| Nobody is told when something is there | That only works for requests made through the bot, and the bot must keep its `state.json` (Docker: the `seerrbot-data` volume). Check the log for `Could not save`. |
| No quality choice appears | The names in `MOVIE_PROFILES`/`TV_PROFILES` must match profiles in Radarr/Sonarr; the log lists the ones it didn't find. With only one name there is nothing to choose, it is just used. |
| The *Jetzt ansehen* button opens an address nobody can reach | Set `JELLYFIN_URL` to Jellyfin's address as people open it. |
| `/jellyfin-zugang` doesn't show up | All of `JELLYFIN_API_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_FRIEND_URL`, `TAILSCALE_SHARE_URL` and an admin setting are needed. |
| „Jellyfin lehnt den API-Schlüssel ab“ or „Diese Jellyfin-Bibliotheken gibt es nicht“ | Check `JELLYFIN_API_KEY`, and that the names in `JELLYFIN_FRIEND_LIBRARIES` match the libraries in Jellyfin. |
| `/filmabend`: „Dem Bot fehlt die Berechtigung Events erstellen“ | Give the bot's role *Create Events* (Server Settings → Roles). |
| No "Geschaut, löschen?" DMs | Needs Jellyfin, admins and `WATCHED_ASK` not `0`. Only what is watched to the end after the bot started counts. Admins by role only (no user IDs) get the question in the approval channel. The admin must allow DMs from server members. |
| `/verfolgen`: „Sonarr findet diese Serie nicht“ | The show has no TVDB id on TMDB yet, so Sonarr can't add it. Try again later or add it in Sonarr by hand, then `/verfolgen` works. |
| `/folgen`, `/verfolgen`, `/kalender` don't show up | They need `SONARR_URL` and `SONARR_API_KEY`. |
| Downloads stay paused | The download brake: less than `BRAKE_FREE_GB` is free. They start again by themselves at 2 GB more; free some space, or set `BRAKE_FREE_GB=0`. |

Logs: `docker logs --tail 50 seerrbot`.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests cover everything except the Discord layer: parsing Seerr's answers, seasons, status texts,
requests, approvals, issues, the limit, the weekly post, the state file and errors, the Sonarr, Radarr,
qBittorrent and Jellyfin calls, the download brake, `/folgen`, `/verfolgen`, `/kalender`, `/zufall`,
`/filmabend` and "Geschaut, löschen?", with fake servers and no network. They need no extra packages.

## License

[MIT](LICENSE)
