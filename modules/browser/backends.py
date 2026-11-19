"""Browser backends. Stdlib only.

FixtureBackend reads file:// fixtures and http://127.0.0.1 pages. CdpBackend is an availability probe only.
Page content is untrusted data: it is parsed and hashed, never interpreted.
"""
import hashlib
import html.parser
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE_ROOTS = (HERE / "tests" / "fixtures", HERE.parent.parent / "examples" / "browser-local" / "site")
CDP_ENV = "BROWSER_CDP_ENDPOINT"
TIMEOUT = 5
TEXT_CAP = 4096
BODY_CAP = 1_000_000
LOOPBACK = re.compile(r"http://127\.0\.0\.1(:[0-9]{1,5})?([/?#][^\s\x00-\x20\x7f-￿]*)?")


class Refused(Exception):
    """A URL, action or operation the backend will not touch."""


class Changed(Exception):
    """The page at submit time is not the approved one; nothing was posted."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Unavailable(Exception):
    """The backend cannot run here."""


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def check_url(url):
    """Return url if it is file:// under a fixture root or http://127.0.0.1[:port]/..., else raise Refused."""
    if not isinstance(url, str):
        raise Refused("url must be a string")
    if url.startswith("file:"):
        path = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path)).resolve()
        if any(root.resolve() in path.parents for root in FILE_ROOTS):
            return url
        raise Refused("file:// is only allowed under the fixture directories")
    p = urllib.parse.urlparse(url)
    if not LOOPBACK.fullmatch(url) or p.hostname != "127.0.0.1" or p.username or p.password:
        raise Refused("only http://127.0.0.1[:port]/ and fixture file:// URLs are allowed")
    return url


def origin(url):
    p = urllib.parse.urlparse(url)
    return (p.scheme, p.netloc)


def same_origin_action(page_url, action):
    """Resolved action url, or Refused when it leaves the page's origin."""
    resolved = urllib.parse.urljoin(page_url, action)
    check_url(resolved)
    if origin(resolved) != origin(page_url):
        raise Refused("form action is not same-origin")
    return resolved


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())  # ignores http_proxy


class _Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.cur, self.text, self.skip = [], None, [], 0
        self.revision, self.marker = None, False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "data-login" in a or (tag == "input" and a.get("type", "").lower() == "password"):
            self.marker = True
        if tag == "meta" and a.get("name") == "revision":
            self.revision = a.get("content", "")
        elif tag in ("script", "style", "title"):
            self.skip += 1
        elif tag == "form":
            self.cur = {"id": a.get("id", ""), "action": a.get("action", ""), "fields": []}
            self.forms.append(self.cur)
        elif tag in ("input", "select", "textarea") and self.cur is not None and a.get("name"):
            ftype = a.get("type", "text").lower() if tag == "input" else tag
            if ftype not in ("submit", "button", "reset", "image"):
                self.cur["fields"].append({"name": a["name"], "type": ftype,
                                           "autocomplete": a.get("autocomplete", "").lower(),
                                           "required": "required" in a})

    def handle_endtag(self, tag):
        if tag in ("script", "style", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag == "form":
            self.cur = None

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def snapshot(url, body, revision_header, login_redirect=False):
    page = _Page()
    if not login_redirect:
        page.feed(body)
        page.close()
    text = " ".join(" ".join(page.text).split())
    forms = [{"id": f["id"], "action": urllib.parse.urljoin(url, f["action"]), "fields": f["fields"]}
             for f in page.forms]
    takeover = bool(page.marker or login_redirect)
    cut = text.encode()[:TEXT_CAP].decode("utf-8", "ignore")
    return {"url": url, "revision": page.revision if page.revision is not None else (revision_header or ""),
            "forms": forms, "dom_sha256": hashlib.sha256(canon({"forms": forms, "text": text})).hexdigest(),
            "takeover": takeover, "untrusted_text": cut}


class Backend:
    name = "base"

    def probe(self):
        raise NotImplementedError

    def survey(self, url):
        raise NotImplementedError

    def submit(self, url, form_id, values, action, pin):
        raise NotImplementedError

    def can_submit(self, url):
        raise NotImplementedError


class FixtureBackend(Backend):
    name = "fixture"

    def probe(self):
        return {"state": "available", "setup": ""}

    def _read(self, url):
        """(final_url, body, X-Revision, login_redirect). Redirects are followed by hand, same origin only."""
        url0 = check_url(url)
        if url0.startswith("file:"):
            path = Path(urllib.request.url2pathname(urllib.parse.urlparse(url0).path))
            return url0, path.read_text("utf-8", "replace")[:BODY_CAP], None, False
        for _ in range(4):
            try:
                with OPENER.open(urllib.request.Request(url, headers={"User-Agent": "ai-architecture-browser"}),
                                 timeout=TIMEOUT) as r:
                    return url, r.read(BODY_CAP).decode("utf-8", "replace"), r.headers.get("X-Revision"), False
            except urllib.error.HTTPError as e:
                if e.code not in (301, 302, 303, 307, 308):
                    raise Refused(f"HTTP {e.code}")
                nxt = check_url(urllib.parse.urljoin(url, e.headers.get("Location", "")))
                if origin(nxt) != origin(url0):
                    raise Refused("redirect leaves the origin")
                if urllib.parse.urlparse(nxt).path.startswith("/login"):
                    return url0, "", None, True
                url = nxt
            except (urllib.error.URLError, OSError) as e:
                raise Refused(f"cannot reach page: {type(e).__name__}")
        raise Refused("too many redirects")

    def survey(self, url):
        final, body, header, login = self._read(url)
        return snapshot(final, body, header, login)

    def can_submit(self, url):
        if check_url(url).startswith("file:"):
            raise Refused("submit over file:// is not allowed")

    def submit(self, url, form_id, values, action, pin):
        """POST values to the approved action only. The fresh fetch is a check against pin (revision, dom_sha256),
        never a source for the action. DEBT: still a gap between this fetch and the POST — close it if a backend can pin state."""
        self.can_submit(url)
        now = self.survey(url)
        for reason, changed in (("takeover", now["takeover"]), ("revision_changed", now["revision"] != pin["revision"]),
                                ("dom_changed", now["dom_sha256"] != pin["dom_sha256"])):
            if changed:
                raise Changed(reason)
        action = same_origin_action(url, action)
        data = urllib.parse.urlencode(values).encode()
        req = urllib.request.Request(action, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with OPENER.open(req, timeout=TIMEOUT) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        except (urllib.error.URLError, OSError) as e:
            raise Refused(f"submit failed: {type(e).__name__}")
        return {"status": status, "after": self.survey(url)}


class CdpBackend(Backend):
    """Availability probe only (U09): reads configuration, opens no socket, never launches or drives Chrome."""
    name = "cdp"

    def probe(self):
        configured = bool(os.environ.get(CDP_ENV))
        setup = (f"Start Chrome with --remote-debugging-port=9222 and set {CDP_ENV}=http://127.0.0.1:9222; "
                 "driving a real browser is not implemented in this release (U10)")
        if configured:
            setup = f"{CDP_ENV} is set, but driving a real browser is not implemented in this release (U10)"
        return {"state": "unavailable", "setup": setup}

    def survey(self, url):
        raise Unavailable("cdp backend is a probe only")

    def submit(self, url, form_id, values, action, pin):
        raise Unavailable("cdp backend is a probe only")

    def can_submit(self, url):
        raise Unavailable("cdp backend is a probe only")


BACKENDS = {"fixture": FixtureBackend, "cdp": CdpBackend}
