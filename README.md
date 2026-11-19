# seerr-discord-bot

Request movies and shows from Discord. The bot searches [Seerr](https://docs.seerr.dev), shows the
poster and whether a title is already there, and requests it with one click. Seerr then hands it
to Radarr or Sonarr as usual.

```text
/film Sint…        (suggestions while you type: Sintel (2010) · Film · ➕ Noch nicht da …)
  Sintel (2010) · ➕ Noch nicht da, kann angefragt werden      [📥 Anfragen] [Abbrechen]
  ⏳ Angefragt! Der Film wartet noch auf die Freigabe durch einen Admin. Ich sage dir hier Bescheid, sobald alles da ist.

  📥 Neue Anfrage von alex: Sintel (2010). Wartet auf Freigabe.      [✅ Freigeben] [✖️ Ablehnen]

/status
  Sintel (2010) · ⬇️ wird geladen, 45 %, noch ca. 20 Min. · von alex

  🍿 @alex Sintel (2010) ist jetzt da. Viel Spass!      [▶️ Jetzt ansehen]
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
- **Limit per person** (optional), e.g. 3 requests per week. Admins have no limit.

**Following up**
- **Approval in Discord.** Requests that wait for approval (also those made in Seerr itself) get
  *Freigeben* / *Ablehnen* buttons that only admins can use. Requests handled in Seerr update the message.
- **"It's there."** The bot remembers who requested what and mentions them when it is in the library,
  with a *Jetzt ansehen* button to Jellyfin. Also when a request was declined.
- **`/status`**: requests waiting for approval, what is being searched or downloaded (with progress and
  time left), who asked for it, and free space.
- **`/neu`**: what came into the library last, with links. Optionally as a **weekly post** in the channel.
- **`/problem <titel>`**: report a problem (picture, sound, subtitles, other) as an issue in Seerr.
- **`/hilfe`**: the commands this person may use, each with an example. Admin commands only for admins.
- **`/abo <serie>`** (needs the Jellyfin setup below): subscribe to a show in the library and get a DM when new
  episodes arrive in Jellyfin, grouped by season. Choosing the show again ends it; without a show it lists
  your subscriptions. Episodes already there when subscribing are not announced.

**Housekeeping**
- **Watches the disk.** Below `MIN_FREE_GB` free, new requests are refused with a clear message, and the
  bot posts once in the channel when space gets low and once when it is fine again.
- **`/löschen <titel>`** (admins only): pick a movie or show from the library and delete it, files
  included, after a second confirmation click. It is reset in Seerr, so it can be requested again later.
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

Seasons and quality are chosen per request; single episodes can't be requested, because Seerr only
requests whole seasons.

## How it works

```mermaid
flowchart LR
    D[Discord channel] -- /film, /serie, /status … --> B[Bot]
    B -- REST API + API key --> S[Seerr]
    S --> R[Radarr / Sonarr]
    B -. optional: /jellyfin-zugang .-> J[Jellyfin]
    B -. state.json .- V[(data volume)]
```

The bot only talks to Discord, to Seerr's API and, if you set it up, to Jellyfin's API. Every two minutes it asks Seerr for new requests to
approve and for finished downloads to announce. It keeps one small file, `state.json` in `DATA_DIR`
(`/data` in Docker): who requested what through Discord, the approval messages it posted, the week
of the last weekly post and who asked for a Jellyfin account. Without it the bot still works, but forgets
that on a restart.

## Setup

### 1. Create the Discord bot

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** → **Reset Token**, copy it.
2. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; bot permissions
   **View Channels**, **Send Messages**, **Embed Links**. Open the link and add the bot to your server.
   No privileged intents are needed.
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
git clone https://github.com/jonesthala/seerr-discord-bot.git seerrbot && cd seerrbot
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
| `DISCORD_ADMIN_ROLE_ID`, `DISCORD_ADMIN_USER_IDS` | empty | Admins: may use `/löschen` and approve or decline requests in Discord; both empty = both off |
| `DISCORD_APPROVAL_CHANNEL_ID` | the bot's channel | Where requests waiting for approval are posted with buttons |
| `MIN_FREE_GB` | `15` | Refuse new requests and warn below this much free space |
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

Logs: `docker logs --tail 50 seerrbot`.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests cover everything except the Discord layer: parsing Seerr's answers, seasons, status texts,
requests, approvals, issues, the limit, the weekly post, the state file and errors, with a fake Seerr and
no network. They need no extra packages.

## License

[MIT](LICENSE)
