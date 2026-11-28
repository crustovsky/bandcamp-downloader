import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from html import unescape as html_unescape
from http.cookies import SimpleCookie
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi import requests


def load_cookies(cookies_str):
    cookies = SimpleCookie()
    try:
        cookies.load(cookies_str)
    except Exception as e:
        raise SystemExit(f"Failed to parse cookies: {e}")
    if len(cookies) == 0:
        # Try parsing as Netscape tab-separated format
        for line in cookies_str.strip().split("\n"):
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split("\t")
            if len(parts) == 7:
                domain, _tailmatch, path, secure, _expires, name, value = parts
                cookie_string = f"{name.strip()}={value.strip()}; Domain={domain.strip()}; Path={path.strip()}"
                if secure == "TRUE":
                    cookie_string += "; Secure"
                cookies.load(cookie_string)
    if not cookies.get("identity"):
        raise SystemExit(
            "Cookie data does not contain an identity value, make sure your "
            "cookies.txt file is valid and you copied it from an "
            "authenticated browser"
        )
    return cookies


def create_session(cookies):
    session = requests.Session(impersonate="chrome")
    for cookie_name, morsel in cookies.items():
        session.cookies.set(cookie_name, morsel.value)
    return session


def extract_pagedata(soup, id_name="pagedata"):
    tag = soup.find("div", id=id_name)
    if (not tag or "data-blob" not in tag.attrs) and soup.title and "Challenge" in (soup.title.string or ""):
        raise SystemExit(
            "Bandcamp answered with a bot challenge page instead of the site. "
            "The cookies may be fine; try another curl-cffi version."
        )
    if not tag or "data-blob" not in tag.attrs:
        raise SystemExit(
            f'Failed to locate <div id="{id_name}"> in HTML. '
            "Check your cookies file."
        )
    return json.loads(html_unescape(tag["data-blob"]))


def get_fan_id(session):
    resp = session.get("https://bandcamp.com")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    blob = extract_pagedata(soup, id_name="HomepageApp")
    fan_id = blob.get("pageContext", {}).get("identity", {}).get("fanId")
    if not fan_id:
        raise SystemExit("Could not find fan_id. Are you logged in?")
    return fan_id


def fetch_collection(session, fan_id):
    items = []
    older_than_token = f"{int(time.time())}::a::"
    url = "https://bandcamp.com/api/fancollection/1/collection_items"

    while True:
        payload = {
            "fan_id": fan_id,
            "count": 50,
            "older_than_token": older_than_token,
        }
        resp = session.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            redownload_urls = data.get("redownload_urls", {})
            sale_item_key = f"{item.get('sale_item_type')}{item.get('sale_item_id')}"
            download_url = redownload_urls.get(sale_item_key)
            items.append({
                "band_name": item.get("band_name", "Unknown Artist"),
                "item_title": item.get("item_title", "Unknown Title"),
                "item_id": item.get("item_id"),
                "sale_item_type": item.get("sale_item_type"),
                "sale_item_id": item.get("sale_item_id"),
                "download_url": download_url,
            })

        if not data.get("more_available"):
            break
        older_than_token = data.get("last_token", "")

    # Bandcamp's pages overlap, so the same purchase comes back more than once.
    unique = {}
    for item in items:
        unique.setdefault((item["sale_item_type"], item["sale_item_id"]), item)
    return list(unique.values())


def display_menu(items, start, page_size):
    end = min(start + page_size, len(items))
    print()
    print("=== Bandcamp Downloader ===")
    print(f"Library ({len(items)} items):")
    for i in range(start, end):
        item = items[i]
        print(f"  {i + 1}. {item['band_name']} - {item['item_title']}")
    print()
    if len(items) > page_size:
        print(f"Showing {start + 1}-{end} of {len(items)}")
    print("[number(s)] Download (e.g. 1,3,5-7)  [n/p] Next/Prev page  [q] Quit")


def parse_selection(choice, max_val):
    indices = set()
    for part in choice.split(","):
        part = part.strip()
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                lo, hi = int(bounds[0]), int(bounds[1])
            except ValueError:
                continue
            for n in range(lo, hi + 1):
                if 1 <= n <= max_val:
                    indices.add(n - 1)
        else:
            try:
                n = int(part)
                if 1 <= n <= max_val:
                    indices.add(n - 1)
            except ValueError:
                continue
    return sorted(indices)


def resolve_download_url(session, download_page_url, item_id):
    resp = session.get(download_page_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    blob = extract_pagedata(soup)

    digital_items = blob.get("digital_items", [])
    target = None
    for di in digital_items:
        if di.get("item_id") == item_id:
            target = di
            break
    if not target and digital_items:
        target = digital_items[0]
    if not target:
        raise SystemExit("No digital items found on download page.")

    downloads = target.get("downloads", {})
    fmt = downloads.get("flac") or downloads.get("mp3-320") or downloads.get("mp3-v0")
    if not fmt:
        raise SystemExit("No downloadable format found.")

    download_url = fmt.get("url")
    if not download_url:
        raise SystemExit("No download URL found in format entry.")

    return download_url


def check_download_stat(session, file_download_url):
    url_parts = urlsplit(file_download_url)
    path_parts = url_parts.path.split("/")
    if path_parts[1] == "download":
        path_parts[1] = "statdownload"
    stat_url = urlunsplit((
        url_parts.scheme, url_parts.netloc,
        "/".join(path_parts), url_parts.query, "",
    ))
    resp = session.get(stat_url)
    resp.raise_for_status()
    body = resp.text.strip()
    if body == "var _statDL_result = { result: 'ok'};":
        return file_download_url
    pattern = re.compile('"([^"]+)":"([^"]+)"')
    for k, v in pattern.findall(body):
        if k == "download_url":
            return v
    return file_download_url


def download_file(session, url, dest_path, progress=True):
    resp = session.get(url, stream=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            f.write(chunk)
            downloaded += len(chunk)
            if total and progress:
                pct = downloaded * 100 // total
                print(f"\r  Downloading... {pct}%", end="", flush=True)
    if progress:
        print()
    else:  # a progress bar is noise in a log
        print(f"  Downloaded {downloaded / 1e6:.0f} MB")


def rename_track(filename):
    match = re.search(r"(\d{2,3} .+)", filename)
    if match:
        return match.group(1)
    return filename


AUDIO_EXTENSIONS = {".flac", ".mp3", ".wav", ".ogg", ".aiff", ".aif", ".m4a", ".opus"}


def extract_and_rename(zip_path, dest):
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir)

        for root, _dirs, files in os.walk(tmpdir):
            for fname in files:
                src = Path(root) / fname
                ext = src.suffix.lower()
                if ext in AUDIO_EXTENSIONS:
                    new_name = rename_track(fname)
                else:
                    new_name = fname
                dst = dest / new_name
                shutil.move(str(src), str(dst))


class LocalDestination:
    def __init__(self, root):
        self.root = Path(root).expanduser()

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def close(self):
        pass

    def describe(self, band_name, item_title):
        return str(self.root / band_name / item_title)

    def has_content(self, band_name, item_title):
        dest = self.root / band_name / item_title
        return dest.exists() and any(dest.iterdir())

    @contextmanager
    def staging(self, band_name, item_title):
        """Yield the local directory the album's files should be written to."""
        dest = self.root / band_name / item_title
        dest.mkdir(parents=True, exist_ok=True)
        yield dest


def sftp_quote(path):
    # sftp batch syntax, unquoted form: backslash-escape whitespace, quotes,
    # the backslash itself, comment markers and glob characters. (Inside
    # double quotes sftp treats an escaped glob character as a literal
    # backslash, so quoting cannot express names like "Album [live]".)
    return re.sub(r"""([\\\s"'#*?\[])""", r"\\\1", path)


class SftpDestination:
    """Uploads albums over SFTP using the system ssh/sftp binaries.

    Going through OpenSSH means ~/.ssh/config, keys, agents and known_hosts all
    work as usual. A single master connection is opened up front (so a password
    or passphrase is asked for once) and every sftp call reuses it.
    """

    def __init__(self, url):
        parts = urlsplit(url)
        user, _, hostport = parts.netloc.rpartition("@")
        if hostport.startswith("["):
            host, _, rest = hostport[1:].partition("]")
            port = rest.lstrip(":")
        else:
            host, _, port = hostport.partition(":")
        if not host:
            raise SystemExit(f"No host in SFTP URL: {url}")
        if port and not port.isdigit():
            raise SystemExit(f"Invalid port in SFTP URL: {url}")

        path = unquote(parts.path)
        if path in ("", "/~"):
            path = "."
        elif path.startswith("/~/"):
            path = path[3:] or "."
        self.root = posixpath.normpath(path)

        self.target = f"{user}@{host}" if user else host
        self.port = port
        self.label = f"sftp://{self.target}"
        self.control_dir = None

    def _remote(self, *parts):
        return posixpath.join(self.root, *parts)

    def _sftp_argv(self, batch_path):
        argv = ["sftp", "-q", "-b", batch_path,
                "-o", f"ControlPath={self.control_dir}/cm"]
        if self.port:
            argv += ["-P", self.port]
        return argv + [self.target]

    def _write_batch(self, commands):
        fd, batch_path = tempfile.mkstemp(suffix=".sftp", dir=self.control_dir)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(commands) + "\n")
        return batch_path

    def _run(self, commands):
        batch_path = self._write_batch(commands)
        try:
            return subprocess.run(self._sftp_argv(batch_path),
                                  capture_output=True, text=True)
        finally:
            os.unlink(batch_path)

    def _connect(self):
        self.control_dir = tempfile.mkdtemp(prefix="bcdl-")
        argv = ["ssh", "-M", "-S", f"{self.control_dir}/cm",
                "-o", "ControlPersist=no", "-f", "-N"]
        if self.port:
            argv += ["-p", self.port]
        if subprocess.run(argv + [self.target]).returncode != 0:
            self.close()
            raise SystemExit(f"Could not connect to {self.target}")

    def prepare(self):
        for tool in ("ssh", "sftp"):
            if not shutil.which(tool):
                raise SystemExit(f"'{tool}' not found; SFTP output needs OpenSSH installed.")
        print(f"Connecting to {self.target}...")
        self._connect()

        # No "mkdir -p" in sftp: create each level, ignoring failures (the "-"
        # prefix), then cd into the result to check it really exists.
        commands = []
        prefix = "/" if self.root.startswith("/") else ""
        for part in self.root.strip("/").split("/"):
            prefix = posixpath.join(prefix, part)
            commands.append(f"-mkdir {sftp_quote(prefix)}")
        commands.append(f"cd {sftp_quote(self.root)}")
        if self._run(commands).returncode != 0:
            self.close()
            raise SystemExit(
                f"Remote directory is not accessible: {self.label}:{self.root}"
            )

    def close(self):
        if not self.control_dir:
            return
        sock = f"{self.control_dir}/cm"
        if os.path.exists(sock):
            subprocess.run(["ssh", "-S", sock, "-O", "exit", self.target],
                           capture_output=True)
        shutil.rmtree(self.control_dir, ignore_errors=True)
        self.control_dir = None

    def describe(self, band_name, item_title):
        return f"{self.label}:{self._remote(band_name, item_title)}"

    def has_content(self, band_name, item_title):
        result = self._run([f"ls -1 {sftp_quote(self._remote(band_name, item_title))}"])
        if result.returncode != 0:
            return False
        return any(line.strip() and not line.startswith("sftp> ")
                   for line in result.stdout.splitlines())

    def _upload(self, stage, band_name, item_title):
        remote_album = self._remote(band_name, item_title)
        commands = [
            f"-mkdir {sftp_quote(self._remote(band_name))}",
            f"-mkdir {sftp_quote(remote_album)}",
        ]
        total = 0
        for root, dirs, files in os.walk(stage):
            dirs.sort()
            rel = Path(root).relative_to(stage)
            remote_dir = posixpath.join(remote_album, *rel.parts)
            for d in dirs:
                commands.append(f"-mkdir {sftp_quote(posixpath.join(remote_dir, d))}")
            for fname in sorted(files):
                local = str(Path(root) / fname)
                remote = posixpath.join(remote_dir, fname)
                commands.append(f"put {sftp_quote(local)} {sftp_quote(remote)}")
                total += 1

        batch_path = self._write_batch(commands)
        try:
            proc = subprocess.Popen(self._sftp_argv(batch_path), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            done = 0
            last_message = ""
            for line in proc.stdout:
                if line.startswith("sftp> put "):
                    done += 1
                    print(f"\r  Uploading... {done}/{total}", end="", flush=True)
                elif line.strip() and not line.startswith("sftp> "):
                    last_message = line.strip()
            returncode = proc.wait()
            if done:
                print()
        finally:
            os.unlink(batch_path)
        if returncode != 0:
            raise RuntimeError(last_message or f"sftp exited with status {returncode}")

    @contextmanager
    def staging(self, band_name, item_title):
        """Yield a local temp directory, then upload its contents on exit."""
        stage = Path(tempfile.mkdtemp(prefix="bcdl-stage-"))
        try:
            yield stage
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        try:
            self._upload(stage, band_name, item_title)
        except (RuntimeError, OSError) as e:
            # Keep the files so a failed upload doesn't cost a re-download.
            raise SystemExit(
                f"  Upload failed: {e}\n  Downloaded files kept in: {stage}"
            )
        shutil.rmtree(stage, ignore_errors=True)


def make_destination(output):
    if output.lower().startswith("sftp://"):
        return SftpDestination(output)
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", output):
        scheme = output.split("://", 1)[0]
        raise SystemExit(
            f"Unsupported output scheme '{scheme}://'. "
            "Use a local path or sftp://[user@]host[:port]/path"
        )
    return LocalDestination(output)


def download_album(session, item, destination, interactive=True):
    """Returns True when the item is in place: downloaded now, or already there."""
    if not item.get("download_url"):
        print("  No download URL available for this item.")
        return False

    band_name, item_title = item["band_name"], item["item_title"]
    where = destination.describe(band_name, item_title)
    if destination.has_content(band_name, item_title):
        if not interactive:
            # An unattended run never touches an album folder that already exists.
            print(f"  {where} already exists. Left as it is.")
            return True
        answer = input(f"  {where} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("  Skipped.")
            return True

    print(f"  Fetching download page...")
    download_url = resolve_download_url(session, item["download_url"], item["item_id"])

    print(f"  Resolving download link...")
    final_url = check_download_stat(session, download_url)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_file(session, final_url, tmp_path, progress=interactive)

        with destination.staging(band_name, item_title) as dest:
            if zipfile.is_zipfile(tmp_path):
                extract_and_rename(tmp_path, dest)
            else:
                # Single file - just move it
                fname = Path(final_url.split("?")[0]).name or "download"
                shutil.move(tmp_path, str(dest / fname))
        print(f"  Saved to: {where}")
        return True
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def interactive_loop(session, items, destination):
    page = 0
    page_size = 10

    while True:
        display_menu(items, page * page_size, page_size)
        choice = input("> ").strip()

        if choice.lower() == "q":
            break
        elif choice.lower() == "n":
            max_page = (len(items) - 1) // page_size
            if page < max_page:
                page += 1
        elif choice.lower() == "p":
            if page > 0:
                page -= 1
        else:
            indices = parse_selection(choice, len(items))
            if not indices:
                print("Invalid input.")
                continue
            for idx in indices:
                item = items[idx]
                print(f"\nDownloading: {item['band_name']} - {item['item_title']}")
                download_album(session, item, destination)


def item_key(item):
    return f"{item.get('sale_item_type')}{item.get('sale_item_id')}"


def load_state(state_path):
    """Keys of the purchases already dealt with, or None before the first sync."""
    if not state_path.exists():
        return None
    return set(json.loads(state_path.read_text())["done"])


def save_state(state_path, done):
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done)}, indent=1))
    tmp.replace(state_path)


def sync(session, items, destination, state_path):
    """Download purchases not seen before. Unattended: asks nothing, overwrites nothing.

    The first sync downloads nothing. It only records what is in the collection now,
    so that a library which already holds older purchases, perhaps under other folder
    names, is not filled with duplicates. Older items: use the interactive mode.
    """
    done = load_state(state_path)
    if done is None:
        save_state(state_path, {item_key(i) for i in items})
        print(f"First sync: noted the {len(items)} purchases you already have. "
              "Only purchases made from now on will be downloaded.")
        return
    new = [i for i in items if item_key(i) not in done]
    print(f"{len(items)} purchases, {len(new)} new.")
    for item in reversed(new):  # oldest first
        print(f"New: {item['band_name']} - {item['item_title']}")
        try:
            if download_album(session, item, destination, interactive=False):
                done.add(item_key(item))
                save_state(state_path, done)
        except (SystemExit, Exception) as e:  # this one is tried again at the next sync
            print(f"  Failed, will retry: {e}")


def sync_once(args):
    cookies_path = Path(args.cookies).expanduser()
    if not cookies_path.exists():
        raise SystemExit(f"Cookies file not found: {cookies_path}")
    destination = make_destination(args.output_dir)
    if args.require_existing and isinstance(destination, LocalDestination):
        root = destination.root
        if not root.is_dir() or not any(root.iterdir()):
            raise SystemExit(f"{root} is missing or empty (disk not mounted?). Nothing done.")
    destination.prepare()
    try:
        session, items = connect(cookies_path)
        sync(session, items, destination, Path(args.state).expanduser())
    finally:
        destination.close()


def main():
    parser = argparse.ArgumentParser(description="Bandcamp Downloader")
    parser.add_argument("--cookies", "-c", default="cookies.txt", help="Path to cookies.txt (default: cookies.txt)")
    parser.add_argument("-O", required=True, dest="output_dir", metavar="OUTPUT",
                        help="Output directory, or sftp://[user@]host[:port]/path to upload over SFTP")
    parser.add_argument("--sync", action="store_true",
                        help="Unattended: download purchases made since the first sync, then exit")
    parser.add_argument("--state", default="state.json", metavar="FILE",
                        help="With --sync: file remembering which purchases are done (default: state.json)")
    parser.add_argument("--every", type=float, metavar="HOURS",
                        help="With --sync: keep running and sync again every HOURS hours")
    parser.add_argument("--require-existing", action="store_true",
                        help="With --sync: do nothing if a local output directory is missing or empty, "
                             "so an unmounted disk is not written to")
    args = parser.parse_args()

    if args.sync:
        while True:
            print(time.strftime("%Y-%m-%d %H:%M"), "sync", flush=True)
            try:
                sync_once(args)
            except (SystemExit, Exception) as e:
                # A missing or expired cookies file lands here. Say so; try again next time.
                print(f"Sync failed: {e}", flush=True)
                if not args.every:
                    raise SystemExit(1)
            if not args.every:
                return
            time.sleep(args.every * 3600)

    cookies_path = Path(args.cookies).expanduser()
    destination = make_destination(args.output_dir)

    if not cookies_path.exists():
        raise SystemExit(f"Cookies file not found: {cookies_path}")
    destination.prepare()
    try:
        run(cookies_path, destination)
    finally:
        destination.close()


def connect(cookies_path):
    """A logged-in session and the collection, newest purchase first."""
    with open(cookies_path, "rt") as f:
        cookies = load_cookies(f.read().strip())
    session = create_session(cookies)
    fan_id = get_fan_id(session)
    return session, fetch_collection(session, fan_id)


def run(cookies_path, destination):
    print("Authenticating and fetching library...")
    session, items = connect(cookies_path)
    print(f"Found {len(items)} items.")

    if not items:
        raise SystemExit("Your collection is empty.")

    interactive_loop(session, items, destination)


if __name__ == "__main__":
    main()
