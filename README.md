# Anime Watch Together

[Русская версия](README.ru.md)

Watch anime together, in sync. Create a room, share the link — and the player
runs second-for-second for everyone. Play, pause, seek and episode switches are
visible to the whole room. Plus a chat and automatic episode tracking.

Video links come from [anime-dl-core](https://github.com/ialakey/anime-dl-core):
AniBoom, Kodik, CVH, Sibnet, Animedia, AniLibria, VK Video, SovetRomantica.
Sign-in goes through Discord with a guild membership check — switched on with a
single setting.

![A room with a shared player, viewer list and chat](docs/screenshots/room.jpg)

---

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Discord setup](#discord-setup)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Development](#development)
- [API](#api)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [License and disclaimer](#license-and-disclaimer)

---

## Features

**Watching together**
- Rooms with a short code and an invite link.
- Shared player state: play, pause, seek, episode and dub switching.
- Two control modes — "everyone" or "host only" — toggled on the fly.
- The server is the source of truth: a late viewer lands on the right second by
  itself, and a reference timestamp is broadcast every few seconds to cure drift.
- Room chat and a live viewer list.
- Public rooms are listed on the home page; private ones are link-only.

**Catalogue**
- Anime search, episode list, choice of dub and player.
- Source is a setting: `animego` (default) or `animedia`.
- Mirror and proxy support for when a source gets blocked or hits Cloudflare.

**Tracking**
- An episode is marked watched automatically past 85% (configurable).
- A personal list with statuses: watching, planned, completed, on hold, dropped.
- Ratings 1–10, notes, a "continue watching" row on the home page, hours watched.

**Authentication**
- Discord OAuth2 with a guild membership check and, optionally, role checks.
- Three ways to verify membership: user token, bot token, or no check at all.
- Turn it off with one setting and the site runs in guest mode (name only).

---

## Screenshots

|  |  |
|---|---|
| **Home** — continue watching and open rooms<br>[![Home](docs/screenshots/home.jpg)](docs/screenshots/home.jpg) | **Search** — catalogue lookup with ratings<br>[![Search](docs/screenshots/search.jpg)](docs/screenshots/search.jpg) |
| **Title page** — episodes, dubs, list controls<br>[![Title page](docs/screenshots/anime.jpg)](docs/screenshots/anime.jpg) | **My list** — statuses, ratings, progress<br>[![My list](docs/screenshots/library.jpg)](docs/screenshots/library.jpg) |

Sign-in when `DISCORD_AUTH_ENABLED=true` — the site only asks Discord for a name,
an avatar and confirmation that the account is in the guild:

[![Sign in with Discord](docs/screenshots/login-discord.jpg)](docs/screenshots/login-discord.jpg)

---

## How it works

```
Browser ──WebSocket──► FastAPI ──► RoomManager (room state, in memory)
   │                      │
   │                      ├──► Catalog ──► anime-dl-core ──► AnimeGO / Animedia
   │                      │                                  └► AniBoom / Kodik / CVH / …
   │                      │
   └──HTTP(S) video──────►└──► StreamProxy ──► player CDN
```

The key piece is the **video proxy**. The direct links a player hands out cannot
simply be dropped into a `<video src>`:

- the CDN only serves the file with the right `Referer`, and a browser will not
  send one;
- the link is bound to the IP that fetched it — that is, to the server;
- links are short-lived.

So the server issues a **signed token** for every link (HMAC over the URL, the
headers and an expiry) and serves the video itself, rewriting HLS playlists on
the fly. Nothing is fetched without a valid signature, so the proxy never
becomes an open relay.

Synchronization: a client reports "I pressed play at 12.5s", the server stores
the position together with the moment it happened and broadcasts it. The
"current" position is computed as `position + (now - updated_at)` while playing.
A client corrects itself when it drifts further than `ROOM_SYNC_TOLERANCE`.

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/ialakey/anime-watch-together.git
cd anime-watch-together
cp .env.example .env         # at minimum, set SECRET_KEY
docker compose up -d --build
```

The site comes up on <http://localhost:8000>. Discord sign-in is off by default:
entering a name is enough.

PostgreSQL instead of SQLite:

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

### Locally, without Docker

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

Python 3.11+ is required. The default database is SQLite at
`./data/anime_watch.db`, and the schema is created on first start.

---

## Discord setup

1. Open <https://discord.com/developers/applications> → **New Application**.
2. The **OAuth2** tab:
   - copy the **Client ID** and **Client Secret**;
   - under **Redirects** add `https://your-domain/auth/discord/callback`
     (for local development, `http://localhost:8000/auth/discord/callback`).
3. Get the guild ID: enable developer mode in Discord
   (*Settings → Advanced → Developer Mode*), then right-click the server →
   **Copy Server ID**.
4. Fill in `.env`:

```dotenv
DISCORD_AUTH_ENABLED=true
DISCORD_CLIENT_ID=1234567890
DISCORD_CLIENT_SECRET=your-secret
DISCORD_GUILD_ID=9876543210
DISCORD_GUILD_CHECK=oauth
BASE_URL=https://your-domain
```

### Membership check modes

| `DISCORD_GUILD_CHECK` | How it checks | What it needs | Sees roles |
|---|---|---|---|
| `oauth` (default) | asks Discord on behalf of the user | nothing beyond the OAuth app | yes, if the `guilds.members.read` scope was granted |
| `bot` | asks a bot that is itself in the guild | `DISCORD_BOT_TOKEN`, the bot on the server, **Server Members** intent | always |
| `off` | no check, any Discord account gets in | — | no |

Restricting access by role:

```dotenv
DISCORD_REQUIRED_ROLE_IDS=111111111111,222222222222
DISCORD_ADMIN_IDS=333333333333
```

Role IDs are copied the same way: right-click a role in server settings →
**Copy ID**. Role checks require `oauth` or `bot`.

> To turn authentication off entirely, set `DISCORD_AUTH_ENABLED=false`. The
> login page then only asks for a name, and the profile lives in that browser's
> cookie.

---

## Configuration

Every setting is read from environment variables or from `.env`. The full list
lives in [`.env.example`](.env.example); the essentials are below.

### Application

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `Anime Watch Together` | Name in the header and page titles. |
| `BASE_URL` | `http://localhost:8000` | Public address. Drives the OAuth redirect and room links. |
| `SECRET_KEY` | random | **Set this in production.** Signs sessions and video links. Changing it invalidates both. |
| `DEBUG` | `false` | Verbose logging. |
| `SESSION_MAX_AGE` | `1209600` | Session lifetime, seconds. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/anime_watch.db` | Connection string. For Postgres: `postgresql+asyncpg://user:pass@host/db`. |

### Discord

| Variable | Default | Description |
|---|---|---|
| `DISCORD_AUTH_ENABLED` | `false` | The master switch for authentication. |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | — | From the OAuth2 tab. |
| `DISCORD_REDIRECT_URI` | `BASE_URL` + `/auth/discord/callback` | Override only if the address differs. |
| `DISCORD_GUILD_ID` | — | The guild that grants access. |
| `DISCORD_GUILD_CHECK` | `oauth` | `oauth` / `bot` / `off`. |
| `DISCORD_BOT_TOKEN` | — | Required for `bot` mode. |
| `DISCORD_REQUIRED_ROLE_IDS` | — | Comma-separated role IDs. |
| `DISCORD_ADMIN_IDS` | — | Site admins; they can close any room. |

### Catalogue and network

| Variable | Default | Description |
|---|---|---|
| `CATALOG_SOURCE` | `animego` | `animego` or `animedia`. |
| `ANIMEGO_MIRROR` | — | Mirror to use when the main domain is blocked (`animego.me`). |
| `ANIMEDIA_BASE_URL` | `https://amd.online` | The Animedia domain — it moves from time to time. |
| `HTTP_PROXY` | — | `http://…` or `socks5://…` (for socks: `pip install "anime-dl-core[socks]"`). |
| `HTTP_TIMEOUT` | `25` | Per-request timeout to a source, seconds. |
| `CATALOG_CACHE_TTL` | `600` | Cache for search and episode lists, seconds. |
| `STREAM_CACHE_TTL` | `240` | Cache for direct links. Keep it small — those links expire. |

### Player and rooms

| Variable | Default | Description |
|---|---|---|
| `STREAM_PROXY_ENABLED` | `true` | Proxy video through the server. Only turn it off if you know why. |
| `STREAM_TOKEN_TTL` | `21600` | Lifetime of a signed link, seconds. |
| `STREAM_MAX_QUALITY` | `1080` | Quality ceiling, pixels of height. |
| `ROOM_IDLE_TIMEOUT` | `10800` | How long an empty room survives, seconds. |
| `ROOM_MAX_MEMBERS` | `25` | Viewer limit per room. |
| `ROOM_DEFAULT_CONTROL` | `everyone` | `everyone` or `host`. |
| `ROOM_SYNC_INTERVAL` | `8` | How often the reference timestamp is broadcast, seconds. |
| `ROOM_SYNC_TOLERANCE` | `1.5` | Drift tolerated before a client corrects itself, seconds. |

### Tracking

| Variable | Default | Description |
|---|---|---|
| `TRACKING_ENABLED` | `true` | Disables the whole "my list" section. |
| `EPISODE_COMPLETED_RATIO` | `0.85` | Share of an episode after which it counts as watched. |
| `PROGRESS_REPORT_INTERVAL` | `15` | How often a client reports its position, seconds. |

---

## Deployment

### docker compose

`docker-compose.yml` runs the app on SQLite with a volume for data. Overlays add
PostgreSQL and a development mode with auto-reload.

```bash
# SQLite (default)
docker compose up -d --build

# PostgreSQL
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build

# development: code mounted in, uvicorn --reload
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

docker compose logs -f app
docker compose down
```

Liveness check: `curl http://localhost:8000/healthz`.

### Behind a reverse proxy

WebSockets need the `Upgrade` headers. An nginx example:

```nginx
server {
    listen 443 ssl http2;
    server_name anime.example.com;

    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # video is streamed — buffering only gets in the way
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

Do not forget `BASE_URL=https://anime.example.com`, or the OAuth redirect will
point somewhere else and the cookie will miss its `Secure` flag.

### Migrations

On SQLite the schema is created at startup. On PostgreSQL, apply Alembic
(the container does this in `docker/entrypoint.sh`):

```bash
alembic upgrade head            # apply
alembic revision --autogenerate -m "what changed"
alembic downgrade -1            # roll the last one back
```

### Scaling

Rooms live **in the process memory**, so the app is built for a **single
worker**. Two workers must not happen: viewers would land in different copies of
the same room. If you outgrow one process:

- vertically — a single process serves hundreds of viewers; the bottleneck is
  usually the video proxy;
- horizontally — several instances behind a load balancer with sticky routing by
  room code (`/room/<code>` and `/ws/room/<code>` of one code to one instance);
- fully shared state over Redis pub/sub is not implemented yet.

---

## Development

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload

ruff check . && ruff format --check .   # lint and formatting
mypy app                                # types
pytest -q                               # tests
```

Tests never touch the network: catalogue sources are stubbed and the database is
in-memory SQLite.

Layout:

```
app/
├── main.py            FastAPI assembly, middleware, error handlers
├── config.py          every setting (pydantic-settings)
├── deps.py            FastAPI dependencies
├── auth/              Discord OAuth2, guild checks, sessions
├── api/               REST (/api/*) and WebSocket (/ws/room/{code})
├── db/                SQLAlchemy models and engine
├── services/
│   ├── catalog.py     anime-dl-core wrapper (search, episodes, players)
│   ├── playback.py    builds the player source
│   ├── rooms.py       rooms, playback state, chat
│   ├── streaming.py   signed links and HLS proxying
│   ├── tracking.py    anime list and per-episode progress
│   └── cache.py       TTL cache
├── web/views.py       pages (Jinja2)
├── templates/         templates
└── static/            css/js/icons, no bundler
```

---

## API

Interactive docs: `/api/docs`. Everything except the login page requires a session.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/anime/search?q=` | search |
| `GET` | `/api/anime/{id}` | title card and episode list |
| `GET` | `/api/anime/{id}/players?episode=` | players and dubs for an episode |
| `GET` | `/api/rooms` | public rooms |
| `POST` | `/api/rooms` | create a room |
| `GET` | `/api/rooms/{code}` | room state |
| `DELETE` | `/api/rooms/{code}` | close a room (host only) |
| `GET/PUT/DELETE` | `/api/tracking/*` | list, progress, ratings |
| `GET` | `/api/stream/playlist?t=` | HLS playlist through the proxy |
| `GET` | `/api/stream/segment?t=` | a segment or a whole file |
| `WS` | `/ws/room/{code}` | room synchronization |

### WebSocket protocol

From the client: `play`, `pause`, `seek`, `sync_request`, `set_source`,
`progress`, `chat`, `settings`, `ping`.
From the server: `state`, `playback`, `sync`, `source`, `members`, `chat`,
`settings`, `loading`, `ack`, `error`, `closed`, `pong`.

Example:

```json
{"type": "play", "position": 132.4}
{"type": "set_source", "anime_id": "naruto-uragannye-hroniki-103", "episode": 3, "player_key": "aniboom::anilibria"}
```

---

## Architecture

**A room** (`services/rooms.py`) holds the playback state, the chosen source, the
viewers and the chat. Mutations run under an `asyncio.Lock`; broadcasts go to
every connection except the initiator, who gets an `ack` instead.

**The catalogue** (`services/catalog.py`) hides two sources behind one interface:
AnimeGO and Animedia. The library is synchronous, so its calls go to a thread
pool (`anyio.to_thread`) and results land in a TTL cache with dogpile protection.

**The proxy** (`services/streaming.py`) issues signed links and rewrites HLS
playlists: every nested URI (variants, segments, encryption keys, `EXT-X-MAP`)
is replaced with a link to the same proxy. `Range` is supported, so seeking
through an mp4 works.

**Tracking** (`services/tracking.py`) creates the list entry on first watch and
moves the episode counter once an episode is finished.

---

## Troubleshooting

**"The source answered with an error — bot protection probably kicked in"**
AnimeGO is behind Cloudflare. Try `ANIMEGO_MIRROR=animego.me`, set `HTTP_PROXY`,
or switch to `CATALOG_SOURCE=animedia`.

**Video will not load, 403 in the console**
The link expired, or the server changed IP. Reload the page — the source is
rebuilt. Make sure `STREAM_PROXY_ENABLED=true`: without the proxy the CDN
answers 403 almost every time.

**Discord refuses: "your account is not in the guild"**
Check `DISCORD_GUILD_ID`, and that you allowed access to your server list while
signing in. If you need role checks, switch to `DISCORD_GUILD_CHECK=bot` and add
the bot to the server with the **Server Members** intent.

**`redirect_uri mismatch`**
The address in the Discord application must match `BASE_URL` +
`/auth/discord/callback` character for character, scheme and port included.

**WebSocket will not connect behind nginx**
`Upgrade`/`Connection` are not being passed through — see the config above.

**Everyone's player drifts apart**
Lower `ROOM_SYNC_TOLERANCE` and `ROOM_SYNC_INTERVAL`. Keep in mind that with
different connection speeds perfect sync is impossible: someone always buffers
longer.

**Rooms disappear after a restart**
By design — they live in memory. The anime list and progress are in the database
and survive.

---

## Limitations

- Room state does not survive a restart and is not shared between workers.
- Opening/ending timestamps are not published by every player, so the
  "skip opening" button only appears when the data exists.
- Downloading episodes is not implemented, even though `anime-dl-core` can emit
  an ffmpeg command for it.
- Voice chat is not planned — that is what Discord is for.

## License and disclaimer

MIT — see [LICENSE](LICENSE).

The project stores and distributes no video: it only surfaces what third-party
players have already published, and proxies the stream because otherwise those
players will not hand it to a browser. Responsibility for running the site lies
with whoever deploys it. Make sure it is legal where you are.
