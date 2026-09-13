# -*- coding: utf-8 -*-

"""
Burst web client
"""

from future.utils import PY3, iteritems

import re
import os
import urllib3
import requests
import antizapret
try:
    import dns.resolver
    platform_can_resolve = True
except:
    platform_can_resolve = False

from elementum.provider import log, get_setting
from time import sleep, time
from urllib3.util import connection
from .utils import encode_dict, translatePath, is_ipv4_address
if PY3:
    from http.cookiejar import LWPCookieJar
    from urllib.parse import urlparse, urlencode
    unicode = str
else:
    from cookielib import LWPCookieJar
    from urllib import urlencode
    from urlparse import urlparse
from kodi_six import xbmcaddon, py2_encode

from requests.packages.urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from requests.cookies import create_cookie

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
if os.name == 'nt':
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

if get_setting("use_custom_user_agent", bool):
    agent = get_setting("custom_user_agent", unicode)
    if agent:
        USER_AGENT = agent
        log.debug("Using custom User Agent: %s" % (USER_AGENT))

PATH_TEMP = translatePath("special://temp")

# FlareSolverr defaults, used when the setting is empty.
FLARESOLVERR_DEFAULT_URL = "http://127.0.0.1:8191/v1"
FLARESOLVERR_DEFAULT_TIMEOUT = 60

# Cloudflare marks challenge responses with this header. Older versions of the
# interstitial do not send it, so the body is checked for known markers as well.
CLOUDFLARE_MITIGATED_HEADER = "Cf-Mitigated"
CLOUDFLARE_CHALLENGE_STATUSES = (403, 503)
CLOUDFLARE_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "_cf_chl_opt",
    "cf_chl_",
    "<title>Just a moment...</title>",
)

# Custom DNS default data
OPENNIC_API_URL = 'https://api.opennicproject.org/geoip/?bare&res=3&adm=3&rnd=true&ipv=4'
OPENNIC_DNS_FALLBACK = ['94.247.43.254', '152.53.15.127', '95.216.99.249']
dns_cache = {}
dns_public_list = ['9.9.9.9', '8.8.8.8', '8.8.4.4']
dns_opennic_list = list(OPENNIC_DNS_FALLBACK)
# Save original DNS resolver
_orig_create_connection = connection.create_connection

# Proxy types
proxy_types = ["socks4",  # socks4 (hostname resolve on client)
    "socks5",  # socks5 (hostname resolve on client)
    "http",
    "https",
    "socks4a",  # socks4 latest version with hostname resolve by proxy
    "socks5h"]  # socks5 latest version with hostname resolve by proxy
elementum_proxy_types_overrides = {'socks4': 'socks4a',
    'socks5': 'socks5h'}

# Disable warning from urllib
urllib3.disable_warnings()

# The bundled requests (2.19.1) consults should_bypass_proxies() for a redirect
# target even when redirects are not followed, because it fills in Response.next.
# A magnet: URL has no hostname, and that None reaches socket.inet_aton, which
# raises TypeError rather than the socket.error the library catches. Upstream
# added this guard in 2.20; apply it to the bundled copy.
_original_should_bypass_proxies = requests.utils.should_bypass_proxies


def _should_bypass_proxies(url, no_proxy=None):
    try:
        if urlparse(url).hostname is None:
            return True
    except Exception:
        return True

    return _original_should_bypass_proxies(url, no_proxy)


requests.utils.should_bypass_proxies = _should_bypass_proxies
requests.sessions.should_bypass_proxies = _should_bypass_proxies


# Kodi settings
proxy_enabled = get_setting("proxy_enabled", bool)
proxy_use_type = get_setting("proxy_use_type", int)
proxy_host = get_setting("proxy_host", unicode)
proxy_port = get_setting("proxy_port", int)
proxy_login = get_setting("proxy_login", unicode)
proxy_password = get_setting("proxy_password", unicode)
proxy_type = get_setting("proxy_type", int)
use_custom_dns = get_setting("use_custom_dns", bool)
public_dns_list = get_setting("public_dns_list", unicode)
use_opennic_dns = get_setting("use_opennic_dns", bool)
use_tor_dns = get_setting("use_tor_dns", bool)
use_elementum_proxy = get_setting("use_elementum_proxy", bool)

# The OpenNIC mirrors of some trackers live under alternative TLDs such as .lib,
# which only resolve through OpenNIC's own name servers. Those are installed only
# when custom DNS is on and a resolver is available on this platform.
custom_dns_active = use_custom_dns and platform_can_resolve

# FlareSolverr is used to pass Cloudflare's "Just a moment..." interstitial.
# It is a separate service (usually a Docker container) driving a real browser,
# that returns the cf_clearance cookie together with the User-Agent it was issued for.
flaresolverr_enabled = get_setting("flaresolverr_enabled", bool)
flaresolverr_url = get_setting("flaresolverr_url", unicode) or FLARESOLVERR_DEFAULT_URL
flaresolverr_timeout = get_setting("flaresolverr_timeout", int) or FLARESOLVERR_DEFAULT_TIMEOUT

def FetchOpenNICDnsServers():
    try:
        response = requests.get(OPENNIC_API_URL, timeout=5, headers={'User-Agent': USER_AGENT})
        response.raise_for_status()
        dns_servers = []
        for line in response.text.splitlines():
            candidate = line.strip()
            if not candidate or not is_ipv4_address(candidate):
                continue
            if candidate in dns_servers:
                continue
            dns_servers.append(candidate)

        if dns_servers:
            log.debug("Loaded %d OpenNIC DNS servers from API" % len(dns_servers))
            return dns_servers
        log.debug("OpenNIC API returned no valid IPv4 DNS servers, using fallback list")
    except Exception as e:
        log.debug("Failed to fetch OpenNIC DNS servers from API: %s" % repr(e))
    return list(OPENNIC_DNS_FALLBACK)


if use_opennic_dns:
    dns_opennic_list = FetchOpenNICDnsServers()


def MyResolver(host):
    if '.' not in host:
        return host

    # Literal addresses have nothing to resolve.
    if is_ipv4_address(host):
        return host

    try:
        return dns_cache[host]
    except KeyError:
        pass

    ip = ResolvePublic(host)
    if not ip and use_opennic_dns:
        ip = ResolveOpennic(host)

    if ip:
        log.debug("Host %s resolved to %s" % (host, ip))
        dns_cache[host] = ip
        return ip
    else:
        return host

def ResolvePublic(host):
    try:
        log.debug("Custom DNS resolving with public DNS for: %s" % host)
        resolver = dns.resolver.Resolver()
        resolver.nameservers = dns_public_list
        answer = resolver.query(host, 'A')
        return answer.rrset.items[0].address
    except:
        return

def ResolveOpennic(host):
    try:
        log.debug("Custom DNS resolving with OpenNIC DNS for: %s" % host)
        resolver = dns.resolver.Resolver()
        resolver.nameservers = dns_opennic_list
        answer = resolver.query(host, 'A')
        return answer.rrset.items[0].address
    except:
        return


class Client:
    """
    Web client class with automatic charset detection and decoding
    """
    def __init__(self, info=None, request_charset='utf-8', response_charset=None, is_api=False):
        self._counter = 0
        self._cookies_filename = ''
        self._cookies = LWPCookieJar()
        self.url = None
        self.user_agent = USER_AGENT
        self.content = None
        self.request_cookies = None
        self.response_cookies = None
        self.status = None
        self.username = None
        self.token = None
        self.passkey = None
        self.info = info
        self.proxy_url = None
        self.use_antizapret = False
        self.antizapret_proxy = None
        self.request_charset = request_charset
        self.response_charset = response_charset
        self.is_api = is_api

        self.use_cookie_sync = False
        self.used_flaresolverr = False

        self.headers = dict()
        self.request_headers = None

        self.session = requests.session()
        self.session.verify = False

        # Enabling retrying on failed requests
        retries = Retry(
            total=3,
            read=2,
            connect=2,
            redirect=3,
            backoff_factor=0.2,
            status_forcelist=[429, 500, 502, 503, 504]
        )

        self.session.mount('http://', HTTPAdapter(max_retries=retries))
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        # self.session = cfscrape.create_scraper()
        # self.scraper = cfscrape.create_scraper()
        # self.session = self.scraper.session()

        global dns_public_list
        dns_public_list = public_dns_list.replace(" ", "").split(",")
        # socket.setdefaulttimeout(60)

        # Parsing proxy information
        proxy = {
            'enabled': proxy_enabled,
            'use_type': proxy_use_type,
            'type': proxy_types[0],
            'host': proxy_host,
            'port': proxy_port,
            'login': proxy_login,
            'password': proxy_password,
        }

        try:
            proxy['type'] = proxy_types[proxy_type]
        except:
            pass

        if use_custom_dns and platform_can_resolve:
            connection.create_connection = patched_create_connection

        if use_elementum_proxy:
            elementum_addon = xbmcaddon.Addon(id='plugin.video.elementum')
            if elementum_addon and elementum_addon.getSetting('internal_proxy_enabled') == "true":
                self.proxy_url = "{0}://{1}:{2}".format("http", "127.0.0.1", "65222")
                if info and "internal_proxy_url" in info:
                    self.proxy_url = info["internal_proxy_url"]
        if proxy['enabled']:
            if proxy['use_type'] == 0 and info and "proxy_url" in info:
                log.debug("Setting proxy from Elementum: %s" % (info["proxy_url"]))

                self.proxy_url = info["proxy_url"]
            elif proxy['use_type'] == 1:
                log.debug("Setting proxy with custom settings: %s" % (repr(proxy)))

                if proxy['login'] or proxy['password']:
                    self.proxy_url = "{0}://{1}:{2}@{3}:{4}".format(proxy['type'], proxy['login'], proxy['password'], proxy['host'], proxy['port'])
                else:
                    self.proxy_url = "{0}://{1}:{2}".format(proxy['type'], proxy['host'], proxy['port'])
            elif proxy['use_type'] == 2 and info and "proxy_url" in info:
                log.debug("Setting proxy with hosts resolve from Elementum: %s" % (info["proxy_url"]))

                proxy_url_scheme_separator = '://'
                elementum_proxy_url_parts = info["proxy_url"].split(proxy_url_scheme_separator)
                elementum_proxy_url_prefix = elementum_proxy_url_parts[0].lower()
                if elementum_proxy_url_prefix in elementum_proxy_types_overrides:
                    self.proxy_url = proxy_url_scheme_separator.join([elementum_proxy_types_overrides[elementum_proxy_url_prefix]] + elementum_proxy_url_parts[1:])
            elif proxy['use_type'] == 3:
                log.debug("Setting proxy to Antizapret proxy")

                self.use_antizapret = True
                self.proxy_url = None
                self.antizapret_proxy = antizapret.AntizapretProxy()
        if self.proxy_url:
            self.session.proxies = {
                'http': self.proxy_url,
                'https': self.proxy_url,
            }

    def _create_cookies(self, payload):
        return urlencode(payload)

    def _locate_cookies(self, url=''):
        cookies_path = os.path.join(PATH_TEMP, 'burst')
        if not os.path.exists(cookies_path):
            try:
                os.makedirs(cookies_path)
            except Exception as e:
                log.debug("Error creating cookies directory: %s" % repr(e))

        return os.path.join(cookies_path, 'common_cookies.jar')

    def _read_cookies(self, url=''):
        self._cookies_filename = self._locate_cookies(url)
        if os.path.exists(self._cookies_filename):
            try:
                self._cookies.load(self._cookies_filename)
            except Exception as e:
                log.debug("Reading cookies error: %s" % repr(e))

    def cookie_exists(self, cookie_name, domain):
        """ Checks whether a cookie covering ``domain`` is present in the jar

        Cookie domains are stored with or without a leading dot depending on where
        they came from (Cookie Sync, FlareSolverr, the site itself), so both forms
        have to match the same host.

        Args:
            cookie_name (str): Name of the cookie to look for
            domain      (str): Host the cookie has to be valid for

        Returns:
            bool: True if such a cookie is present
        """
        domain = (domain or '').lower().lstrip('.')

        for cookie in self._cookies:
            if cookie.name != cookie_name:
                continue

            cookie_domain = (cookie.domain or '').lower().lstrip('.')
            if not cookie_domain:
                continue

            if domain == cookie_domain or domain.endswith('.' + cookie_domain):
                return True

        return False

    def add_cookie(self, cookie):
        cookie_obj = create_cookie(domain=cookie["domain"], name=cookie["name"], value=cookie["value"], path=cookie["path"], secure=cookie["secure"], expires=cookie["expirationDate"], discard=False, rest=cookie["rest"])
        self._cookies.set_cookie(cookie_obj)

    def save_cookies(self):
        self._cookies_filename = self._locate_cookies(self.url)

        try:
            self._cookies.save(self._cookies_filename)
        except Exception as e:
            log.debug("Saving cookies error: %s" % repr(e))

    def _good_spider(self):
        self._counter += 1
        if self._counter > 1:
            sleep(0.25)

    def cookies(self):
        """ Saved client cookies

        Returns:
            list: A list of saved Cookie objects
        """
        return self._cookies

    def is_cloudflare_challenge(self):
        """ Detects Cloudflare's interstitial ("Just a moment...") in the last response

        Returns:
            bool: True if the last response was a Cloudflare challenge
        """
        if self.status not in CLOUDFLARE_CHALLENGE_STATUSES:
            return False

        try:
            if self.headers and self.headers.get(CLOUDFLARE_MITIGATED_HEADER, '').lower() == 'challenge':
                return True
        except Exception:
            pass

        if not self.content:
            return False

        for marker in CLOUDFLARE_CHALLENGE_MARKERS:
            if marker in self.content:
                return True

        return False

    def solve_cloudflare_challenge(self, url):
        """ Asks FlareSolverr to pass the Cloudflare challenge for ``url``

        The cf_clearance cookie is bound to the pair (public IP, User-Agent), so the
        User-Agent reported by FlareSolverr is adopted for all further requests.

        Args:
            url (str): The URL that returned a challenge

        Returns:
            bool: Whether or not clearance cookies were obtained
        """
        log.info("Solving Cloudflare challenge for %s via FlareSolverr at %s" % (repr(url), repr(flaresolverr_url)))

        payload = {
            'cmd': 'request.get',
            'url': url,
            'maxTimeout': flaresolverr_timeout * 1000,
        }

        try:
            # A plain session: FlareSolverr is a local service and must not be
            # reached through the proxy configured for the trackers.
            solver = requests.Session()
            solver.trust_env = False
            with solver.post(flaresolverr_url, json=payload, timeout=flaresolverr_timeout + 15) as response:
                data = response.json()
        except Exception as e:
            log.error("FlareSolverr request failed with: %s" % repr(e))
            return False

        if data.get('status') != 'ok':
            log.error("FlareSolverr could not solve the challenge: %s" % repr(data.get('message')))
            return False

        solution = data.get('solution') or {}

        user_agent = solution.get('userAgent')
        if user_agent:
            log.debug("Using User-Agent reported by FlareSolverr: %s" % repr(user_agent))
            self.user_agent = user_agent
            change_agent(user_agent)

        added = 0
        expires = int(time()) + 86400
        for cookie in solution.get('cookies') or []:
            if 'name' not in cookie or 'value' not in cookie:
                continue

            expiration_date = cookie.get('expires') or 0
            if expiration_date <= 0:
                expiration_date = expires

            try:
                self.add_cookie({
                    'domain': cookie.get('domain') or urlparse(url).netloc,
                    'name': cookie['name'],
                    'value': cookie['value'],
                    'path': cookie.get('path') or '/',
                    'secure': bool(cookie.get('secure')),
                    'expirationDate': int(expiration_date),
                    'rest': {'HttpOnly': bool(cookie.get('httpOnly'))},
                })
                added += 1
            except Exception as e:
                log.debug("Could not store cookie %s: %s" % (repr(cookie.get('name')), repr(e)))

        if not added:
            log.error("FlareSolverr returned no usable cookies")
            return False

        log.info("FlareSolverr returned %d cookies" % added)
        self.save_cookies()

        return True

    def open(self, url, language='en', post_data=None, get_data=None, headers=None, allow_redirects=True, _retry=False):
        """ Opens a connection to a webpage and saves its HTML content in ``self.content``

        Args:
            url        (str): The URL to open
            language   (str): The language code for the ``Content-Language`` header
            post_data (dict): POST data for the request
            get_data  (dict): GET data for the request
            allow_redirects (bool): Whether to follow redirects. Turn it off when the
                target may redirect to a scheme requests cannot prepare, such as magnet:
        """

        if get_data:
            url += '?' + urlencode(get_data)

        if self.use_antizapret:
            parsed = urlparse(url)
            proxy = self.antizapret_proxy.detect(host=parsed.netloc)
            if proxy:
                log.debug("Detected Antizapret proxy for %s: %s" % (parsed.netloc, proxy))
                self.session.proxies = {
                    'http': proxy,
                    'https': proxy,
                }

        log.debug("Opening URL: %s" % repr(url))
        if self.session.proxies:
            log.debug("Proxies: %s" % (repr(self.session.proxies)))

        self._read_cookies(url)
        self.session.cookies = self._cookies

        # log.debug("Cookies for %s: %s" % (repr(url), repr(self._cookies)))

        # Default headers for any request. Pretend like we are the usual browser.
        req_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7,uk;q=0.6,pl;q=0.5',
            'Cache-Control': 'max-age=0',
            'Content-Language': language,
            'Origin': url,
            'Referer': url,
            'User-Agent': self.user_agent
        }

        # Remove referer for API providers
        if self.is_api:
            del req_headers['Referer']

        # If headers passed to open() call - we overwrite headers.
        if headers:
            for key, value in iteritems(headers):
                if key == ':path':
                    u = urlparse(url)
                    value = u.path
                if value:
                    req_headers[key] = value
                elif key.capitalize() in req_headers:
                    del req_headers[key.capitalize()]

        if self.token:
            req_headers["Authorization"] = self.token

        req = None
        if post_data:
            req = requests.Request('POST', url, data=post_data, headers=req_headers)
        else:
            req = requests.Request('GET', url, headers=req_headers)

        prepped = self.session.prepare_request(req)
        self.request_headers = prepped.headers

        try:
            self._good_spider()
            with self.session.send(prepped, allow_redirects=allow_redirects) as response:
                self.headers = response.headers
                self.status = response.status_code
                self.url = response.url

                if self.response_charset:
                    self.content = response.content.decode(self.response_charset, 'ignore')
                else:
                    self.content = response.text
                self.request_cookies = response.request.headers.get('Cookie')
                self.response_cookies = response.cookies.get_dict()

        except requests.exceptions.InvalidSchema as e:
            # If link points to a magnet: then it can be used as a content
            matches = re.findall('No connection adapters were found for \'(.*?)\'', str(e))
            if matches:
                self.content = matches[0]
                return True

            import traceback
            log.error("%s failed with %s:" % (repr(url), repr(e)))
            map(log.debug, traceback.format_exc().split("\n"))
        except Exception as e:
            import traceback
            log.error("%s failed with %s:" % (repr(url), repr(e)))
            map(log.debug, traceback.format_exc().split("\n"))

        if flaresolverr_enabled and not _retry and self.is_cloudflare_challenge():
            log.info("Cloudflare challenge detected for %s" % repr(url))
            if self.solve_cloudflare_challenge(url):
                self.used_flaresolverr = True
                # get_data is already part of url at this point, do not append it twice.
                return self.open(url, language=language, post_data=post_data, headers=headers, allow_redirects=allow_redirects, _retry=True)

        log.debug("Status for %s : %s" % (repr(url), str(self.status)))
        if self.status != 200:
            log.debug("Failed response content for %s : %s" % (repr(url), str(self.content)))

        return self.status == 200

    def login(self, root_url, url, data, headers, fails_with, prerequest=None):
        """ Login wrapper around ``open``

        Args:
            url        (str): The URL to open
            data      (dict): POST login data
            fails_with (str): String that must **not** be included in the response's content

        Returns:
            bool: Whether or not login was successful
        """
        if not url.startswith('http'):
            url = root_url + url

        if prerequest:
            if not prerequest.startswith('http'):
                prerequest = root_url + prerequest
            log.debug("Running prerequest to %s" % (prerequest))
            self.open(py2_encode(prerequest), headers=headers)

        if self.open(py2_encode(url), post_data=encode_dict(data, self.request_charset), headers=headers):
            try:
                if fails_with and re.search(fails_with, self.content):
                    self.status = 'Wrong username or password'
                    return False
            except Exception as e:
                log.debug("Login failed with: %s" % e)
                try:
                    if re.search(fails_with, self.content.decode('utf-8')):
                        self.status = 'Wrong username or password'
                        return False
                except:
                    return False

            return True

        return False

def patched_create_connection(address, *args, **kwargs):
    """Wrap urllib3's create_connection to resolve the name elsewhere"""
    # resolve hostname to an ip address; use your own
    # resolver here, as otherwise the system resolver will be used.
    host, port = address
    log.debug("Custom resolver: %s --- %s --- %s" % (host, port, repr(address)))
    hostname = MyResolver(host)

    return _orig_create_connection((hostname, port), *args, **kwargs)

def change_agent(userAgent):
    global USER_AGENT
    USER_AGENT = userAgent
