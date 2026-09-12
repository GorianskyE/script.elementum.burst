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
   `docker-compose.jackett-only.yml` to that project instead of starting a
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

* `indexers/all/results` searches every configured indexer. Replace `all` with
  `kinozal` to search only Kinozal.
* Sizes arrive as a byte count and seeders/leechers as integers, so no parsing
  hints are needed; Jackett already subtracts seeders from `Peers`.
* `Category[]` is the exact query key Jackett parses (`ResultsController.cs`),
  and it takes a comma separated list. `2000` is movies, `5000` TV, `5070` anime.
* Magnet-only indexers (for example **Kinozal (M)**) return no `Link`, but the
  `InfoHash` mapping keeps those results usable.
* In the Kinozal indexer settings, turning **Strip Cyrillic Letters** off keeps
  release names as they appear on the site, which reads better in Elementum.

### Timeouts

Jackett's **FlareSolverr Max Timeout** defaults to `55000` ms
(`ServerConfig.cs`). A Cloudflare solve from a datacenter IP regularly takes
longer than that, so raise it to `180000` before concluding that the challenge
cannot be passed. The same applies to Burst's own FlareSolverr timeout: 60
seconds is enough from a home connection, a VPS usually needs 150.
