# Kinozal behind Cloudflare: FlareSolverr and Jackett

Since Kinozal put a Cloudflare managed challenge in front of `browse.php` and
`takelogin.php`, a plain HTTP client cannot log in any more: both paths answer
`403` with `Cf-Mitigated: challenge` while the site root still answers `200`.

There are two independent ways out, and they can be used together.

## 1. Native FlareSolverr support (fixes the built-in Kinozal provider)

Burst detects the challenge and asks [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)
to pass it. FlareSolverr drives a real browser and returns the `cf_clearance`
cookie plus the User-Agent it was issued for; Burst stores both and retries the
request once.

Start the solver (see `docker-compose.yml` next to this file), then in
**Burst → General → FlareSolverr**:

| Setting | Value |
| --- | --- |
| Use FlareSolverr to pass Cloudflare challenges | on |
| FlareSolverr URL | `http://<solver-host>:8191/v1` |
| FlareSolverr timeout | 60 |

**The solver has to share its public IP with Kodi.** Cloudflare issues
`cf_clearance` for the pair (public IP, User-Agent), so a solver running on a VPS
hands Kodi a cookie that Cloudflare will reject at home. Run it on the same LAN,
or route Burst through a proxy on the same host (Burst → Proxy).

Nothing changes when the setting is off, and a solver that is unreachable or
fails simply leaves the original response in place.

## 2. Jackett as a custom provider

Jackett keeps the Cloudflare handling entirely on its own side: it talks to
FlareSolverr itself, and Burst only sees a local JSON API. This also brings in
every other indexer configured in Jackett.

1. Start Jackett and FlareSolverr (`docker compose up -d`). If a FlareSolverr
   container is already running for something else, add only the service from
   `docker-compose.override.yml` to that project instead of starting a
   second headless browser.
2. Open `http://<host>:9117`, click **Add indexer**, add **Kinozal**, and enter
   your site credentials. Set **FlareSolverr API URL** in Jackett's settings to
   `http://flaresolverr:8191` (or the host address if the two are not in the same
   compose project).
3. Press the indexer's search button in Jackett's own UI first. If results show
   up there, the Cloudflare side is solved; if they do not, no Kodi configuration
   will help.
4. Copy the **API Key** from the top right of Jackett's page.
5. Copy `jackett.json` into Burst's profile directory:

   ```
   <kodi>/userdata/addon_data/script.elementum.burst/providers/jackett.json
   ```

   Replace `JACKETT_HOST:9117` with the host and port of your Jackett instance
   and `JACKETT_APIKEY` with the API key. Restart Kodi.

Providers dropped into that directory are loaded as custom providers and are
always enabled, so no entry appears in Burst's provider list.

### Notes on the definition

* `JACKETT_INDEXER` is the id of a single indexer, for example `kinozal`.
  `all` also works, but **it waits for the slowest indexer in the instance**:
  measured on one instance, Kinozal answered in 287 ms while RuTracker took
  40 445 ms, and `all` therefore took 40.6 s - far past the point where
  Elementum gives up on a provider ("Provider ... was too slow. Ignored.").
  Name one indexer per provider definition, and only use `all` when every
  configured indexer is fast. There is also no point in routing a tracker
  through Jackett when Burst already has a native provider for it.
* Sizes arrive as a byte count and seeders/leechers as integers, so no parsing
  hints are needed; Jackett already subtracts seeders from `Peers`.
* `Category[]` is the exact query key Jackett parses (`ResultsController.cs`),
  and it takes a comma separated list. `2000` is movies, `5000` TV, `5070` anime.
* Magnet-only indexers (for example **Kinozal (M)**) return no `Link`, but the
  `InfoHash` mapping keeps those results usable.
* In the Kinozal indexer settings, turning **Strip Cyrillic Letters** off keeps
  release names as they appear on the site, which reads better in Elementum.

### Reaching Jackett without a VPN client on the Kodi device

Kodi has to reach Jackett's HTTP port, and a Kodi provider addon - this one or
any other - cannot change that. When Jackett runs on a VPS and the Kodi device
must stay free of VPN clients, put a relay on a machine that is already on both
networks, typically a NAS:

1. On the NAS, forward a LAN port to the Jackett address. On DSM 7 this is
   Control Panel -> Login Portal -> Advanced -> Reverse Proxy: source
   `HTTP / * / 9117`, destination `HTTP / <jackett-address> / 9117`.
2. In Jackett, set **Base URL override** to `http://<nas-lan-address>:9117`.
   `ServerService.GetServerUrl()` checks that setting before falling back to the
   request's Host header, so every `.torrent` link Jackett hands out points at
   the relay rather than at an address the Kodi device cannot resolve.
3. Point `base_url` in the provider definition at the NAS address too.

The Kodi device then talks to one LAN address and needs nothing installed.
Note that Base URL override applies to every client, so links fetched through
any other route (an SSH tunnel, for instance) will also point at the relay.

### One definition per indexer, and trackers with a download quota

A provider definition names one indexer, and Burst runs every provider in its
own thread, so a slow tracker delays only itself. Give each indexer its own
entry (the same JSON file can hold several) rather than pointing one definition
at `all`.

Some trackers cap how many .torrent files a user may download per day. Jackett
offers those as two indexers: one serving torrent files with the user's passkey,
one serving magnet links. The magnet variant resolves the hash lazily, so its
search results carry only a link to Jackett's `/dl/` endpoint, which answers
with a redirect to `magnet:`.

Set `"subpage": true` on such a definition and Burst follows that link and hands
Elementum the magnet, leaving the quota untouched - the same thing the built-in
HTML providers do through their sub-page step. Results are cached per search, so
each link is resolved once. Leave `subpage` off for indexers without a quota:
resolving costs one request per result and buys nothing there.

### Timeouts

Jackett's **FlareSolverr Max Timeout** defaults to `55000` ms
(`ServerConfig.cs`). A Cloudflare solve from a datacenter IP regularly takes
longer than that, so raise it to `180000` before concluding that the challenge
cannot be passed. The same applies to Burst's own FlareSolverr timeout: 60
seconds is enough from a home connection, a VPS usually needs 150.
