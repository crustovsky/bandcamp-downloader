import argparse
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from html import unescape as html_unescape
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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

    return items


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


def download_file(session, url, dest_path):
    resp = session.get(url, stream=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                print(f"\r  Downloading... {pct}%", end="", flush=True)
    print()


def rename_track(filename):
    match = re.search(r"(\d{2,3} .+)", filename)
    if match:
        return match.group(1)
    return filename


AUDIO_EXTENSIONS = {".flac", ".mp3", ".wav", ".ogg", ".aiff", ".aif", ".m4a", ".opus"}


def extract_and_rename(zip_path, output_dir, band_name, item_title):
    dest = Path(output_dir) / band_name / item_title
    dest.mkdir(parents=True, exist_ok=True)

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

    print(f"  Extracted to: {dest}")


def download_album(session, item, output_dir):
    if not item.get("download_url"):
        print("  No download URL available for this item.")
        return

    dest = Path(output_dir) / item["band_name"] / item["item_title"]
    if dest.exists() and any(dest.iterdir()):
        answer = input(f"  {dest} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("  Skipped.")
            return

    print(f"  Fetching download page...")
    download_url = resolve_download_url(session, item["download_url"], item["item_id"])

    print(f"  Resolving download link...")
    final_url = check_download_stat(session, download_url)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_file(session, final_url, tmp_path)

        if zipfile.is_zipfile(tmp_path):
            extract_and_rename(tmp_path, output_dir, item["band_name"], item["item_title"])
        else:
            # Single file - just copy it
            dest = Path(output_dir) / item["band_name"] / item["item_title"]
            dest.mkdir(parents=True, exist_ok=True)
            fname = Path(final_url.split("?")[0]).name or "download"
            shutil.move(tmp_path, str(dest / fname))
            print(f"  Saved to: {dest / fname}")
            return
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def interactive_loop(session, items, output_dir):
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
                download_album(session, item, output_dir)


def main():
    parser = argparse.ArgumentParser(description="Bandcamp Downloader")
    parser.add_argument("--cookies", "-c", default="cookies.txt", help="Path to cookies.txt (default: cookies.txt)")
    parser.add_argument("-O", required=True, dest="output_dir", help="Output directory")
    args = parser.parse_args()

    cookies_path = Path(args.cookies).expanduser()
    output_dir = Path(args.output_dir).expanduser()

    if not cookies_path.exists():
        raise SystemExit(f"Cookies file not found: {cookies_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading cookies...")
    with open(cookies_path, "rt") as f:
        cookies_str = f.read().strip()
    cookies = load_cookies(cookies_str)

    print("Creating session...")
    session = create_session(cookies)

    print("Authenticating...")
    fan_id = get_fan_id(session)
    print(f"Logged in (fan_id: {fan_id})")

    print("Fetching library...")
    items = fetch_collection(session, fan_id)
    print(f"Found {len(items)} items.")

    if not items:
        raise SystemExit("Your collection is empty.")

    interactive_loop(session, items, str(output_dir))


if __name__ == "__main__":
    main()
