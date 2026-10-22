"""Downloads the Archipelago runtime and the custom games (.apworld) released in a GitHub organization.

    python sources.py runtime DEST [--version TAG]   # latest Archipelago release unless TAG is given
    python sources.py worlds DEST [--org ORG]        # latest release *.apworld of every public repo in ORG

Set GITHUB_TOKEN to raise the GitHub API rate limit.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API = "https://api.github.com"
ARCHIPELAGO = "ArchipelagoMW/Archipelago"
WORLDS_ORG = "mahnkepelago"
WORLDS_MANIFEST = "worlds.json"
RUNTIME_REQUIRED = ("Generate.py", "MultiServer.py", "worlds", "LICENSE")
TIMEOUT = 60


def _request(url: str) -> Request:
    headers = {"User-Agent": "multiworld-by-vincent", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


def get_json(url: str):
    with urlopen(_request(url), timeout=TIMEOUT) as response:
        return json.load(response)


def download(url: str, target: Path) -> None:
    with urlopen(_request(url), timeout=TIMEOUT) as response, open(target, "wb") as output:
        shutil.copyfileobj(response, output)


def safe_tag(tag: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tag or ""):
        raise ValueError(f"Unexpected release tag: {tag!r}")
    return tag


# ---------------------------------------------------------------- Archipelago

def latest_archipelago_tag() -> str:
    return safe_tag(get_json(f"{API}/repos/{ARCHIPELAGO}/releases/latest")["tag_name"])


def extract_runtime(archive: Path, target: Path, version: str) -> Path:
    """Extracts a source archive (with or without one top-level folder) into target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=target.parent) as temporary:
        staging = Path(temporary) / "x"
        staging.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(staging, filter="data")
        entries = list(staging.iterdir())
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
        for name in RUNTIME_REQUIRED:
            if not (root / name).exists():
                raise ValueError(f"Archipelago {version} is missing {name}")
        (root / "custom_worlds").mkdir(exist_ok=True)
        (root / "ARCHIPELAGO_VERSION").write_text(version + "\n")
        if target.exists():
            shutil.rmtree(target)
        root.rename(target)
    return target


def download_runtime(tag: str, target: Path) -> Path:
    tag = safe_tag(tag)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=target.parent) as temporary:
        archive = Path(temporary) / "archipelago.tar.gz"
        print(f"Downloading Archipelago {tag}", flush=True)
        download(f"https://github.com/{ARCHIPELAGO}/archive/refs/tags/{tag}.tar.gz", archive)
        return extract_runtime(archive, target, tag)


def runtime_version(runtime: Path) -> str | None:
    marker = Path(runtime) / "ARCHIPELAGO_VERSION"
    if marker.is_file():
        return marker.read_text().strip()
    match = re.search(r'^__version__ = "([^"]+)"', (Path(runtime) / "Utils.py").read_text()
                      if (Path(runtime) / "Utils.py").is_file() else "", re.M)
    return match.group(1) if match else None


# ---------------------------------------------------------------- Custom games

def parse_checksums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match:
            sums[Path(match.group(2).strip()).name] = match.group(1).lower()
    return sums


def org_repositories(org: str) -> list[dict]:
    repos, page = [], 1
    while True:
        batch = get_json(f"{API}/orgs/{org}/repos?type=public&per_page=100&page={page}")
        repos.extend(batch)
        if len(batch) < 100:
            return sorted((repo for repo in repos if not repo.get("archived") and not repo.get("disabled")),
                          key=lambda repo: repo["full_name"].lower())
        page += 1


def released_worlds(org: str) -> list[dict]:
    """Every *.apworld asset of each public repository's latest release in the organization."""
    worlds = []
    for repo in org_repositories(org):
        try:
            release = get_json(f"{API}/repos/{repo['full_name']}/releases/latest")
        except HTTPError as error:
            if error.code == 404:
                print(f"  {repo['full_name']}: no release, skipped", flush=True)
                continue
            raise
        assets = {asset["name"]: asset["browser_download_url"] for asset in release.get("assets", [])}
        names = sorted(name for name in assets if name.lower().endswith(".apworld"))
        if not names:
            print(f"  {repo['full_name']} {release['tag_name']}: no .apworld asset, skipped", flush=True)
            continue
        worlds.extend({"repo": repo["full_name"], "tag": release["tag_name"], "file": name,
                       "url": assets[name], "checksums": assets.get("SHA256SUMS")} for name in names)
    return worlds


def fetch_worlds(org: str, target: Path) -> list[dict]:
    """Replaces target with the organization's released worlds and a worlds.json manifest."""
    worlds = released_worlds(org)
    files = [world["file"] for world in worlds]
    duplicates = sorted({name for name in files if files.count(name) > 1})
    if duplicates:
        raise ValueError(f"Several repositories in {org} release {', '.join(duplicates)}")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".worlds-", dir=target.parent))
    try:
        sums_cache, manifest = {}, []
        for world in worlds:
            path = staging / world["file"]
            download(world["url"], path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if world["checksums"]:
                if world["checksums"] not in sums_cache:
                    listing = staging / ".SHA256SUMS"
                    download(world["checksums"], listing)
                    sums_cache[world["checksums"]] = parse_checksums(listing.read_text())
                    listing.unlink()
                expected = sums_cache[world["checksums"]].get(world["file"])
                if expected and expected != digest:
                    raise ValueError(f"{world['repo']} {world['tag']}: {world['file']} does not match SHA256SUMS")
            manifest.append({"repo": world["repo"], "tag": world["tag"], "file": world["file"], "sha256": digest})
            print(f"  {world['repo']} {world['tag']}: {world['file']}", flush=True)
        (staging / WORLDS_MANIFEST).write_text(json.dumps({"org": org, "worlds": manifest}, indent=2) + "\n")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def installed_worlds(directory: Path) -> list[dict]:
    manifest = Path(directory) / WORLDS_MANIFEST
    try:
        return json.loads(manifest.read_text())["worlds"]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser("runtime")
    runtime.add_argument("dest", type=Path)
    runtime.add_argument("--version", default="latest")
    worlds = commands.add_parser("worlds")
    worlds.add_argument("dest", type=Path)
    worlds.add_argument("--org", default=WORLDS_ORG)
    args = parser.parse_args()
    if args.command == "runtime":
        tag = latest_archipelago_tag() if args.version in ("", "latest") else safe_tag(args.version)
        download_runtime(tag, args.dest)
        print(f"Archipelago {tag} in {args.dest}")
    else:
        print(f"Custom games released in {args.org}:", flush=True)
        found = fetch_worlds(args.org, args.dest)
        print(f"{len(found)} custom game(s) in {args.dest}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        sys.exit(f"sources: {error}")
