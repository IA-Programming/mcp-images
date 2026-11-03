"""Render PDF pages and install PyMuPDF on demand without importing it at module load time."""

import hashlib
import importlib
import logging
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

logger = logging.getLogger("image-mcp")

PYMUPDF_DOWNLOAD_SOURCES: list[tuple[str, str]] = [
    ("PyPI", "https://pypi.org"),
    ("Aliyun", "https://mirrors.aliyun.com/pypi"),
    ("USTC", "https://mirrors.ustc.edu.cn/pypi"),
    ("Tencent Cloud", "https://mirrors.cloud.tencent.com/pypi"),
    ("Huawei Cloud", "https://repo.huaweicloud.com/repository/pypi"),
    ("Tsinghua", "https://pypi.tuna.tsinghua.edu.cn"),
]
_pymupdf_thread_lock = threading.Lock()
_pymupdf_ready = False
_fitz = None


def _select_compatible_wheel(files: list[dict]) -> dict | None:
    """Select a wheel compatible with this interpreter and platform; never fall back to an incompatible wheel."""
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename
    from packaging.version import Version

    tag_priority = {tag: index for index, tag in enumerate(sys_tags())}
    candidates: list[tuple[int, dict]] = []
    for file in files:
        filename = file.get("filename", "")
        if file.get("packagetype", "bdist_wheel") != "bdist_wheel" or not file.get("url"):
            continue
        try:
            _, version, _, wheel_tags = parse_wheel_filename(filename)
            if not Version("1.24") <= version < Version("1.27"):
                continue
        except Exception:
            continue
        matches = [tag_priority[tag] for tag in wheel_tags if tag in tag_priority]
        if not matches:
            continue
        digest = (file.get("digests") or {}).get("sha256", "")
        candidates.append((min(matches), {
            "url": file["url"],
            "size": file.get("size", 0),
            "filename": filename,
            "sha256": digest,
        }))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _query_source_wheel_info(source_name: str, root_url: str, timeout_seconds: float = 8) -> dict | None:
    """Query one source for wheel metadata, using the JSON API first and the simple index as a fallback."""
    import json
    import re
    from html import unescape
    from urllib.parse import parse_qs, unquote, urljoin, urlparse
    from urllib.request import Request, urlopen

    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename
    from packaging.version import Version

    headers = {"User-Agent": "pip/24.0"}
    try:
        with urlopen(Request(f"{root_url}/pypi/pymupdf/json", headers=headers), timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode())
        selected = _select_compatible_wheel(data.get("urls", []))
        if selected:
            selected["source"] = source_name
            return selected
    except Exception:
        pass

    try:
        simple_url = f"{root_url}/simple/pymupdf/"
        with urlopen(Request(simple_url, headers=headers), timeout=timeout_seconds) as response:
            page = response.read().decode("utf-8", errors="replace")
        compatible_tags = set(sys_tags())
        links = re.findall(r'href=["\']([^"\']+)["\']', page, re.IGNORECASE)
        for link in reversed(links):
            link = unescape(link)
            path = urlparse(link).path
            filename = unquote(path.rsplit("/", 1)[-1])
            if not filename.endswith(".whl"):
                continue
            try:
                _, version, _, wheel_tags = parse_wheel_filename(filename)
                if not Version("1.24") <= version < Version("1.27"):
                    continue
            except Exception:
                continue
            if not wheel_tags.intersection(compatible_tags):
                continue
            digest = parse_qs(urlparse(link).fragment).get("sha256", [""])[0]
            return {
                "source": source_name,
                "url": urljoin(simple_url, link),
                "size": 0,
                "filename": filename,
                "sha256": digest,
            }
    except Exception:
        pass
    return None


def _source_download_process_main(source_name: str, root_url: str, subdirectory: str, directory: str, result_queue) -> None:
    """Query metadata and download in a dedicated process so slow sources can be terminated on every platform."""
    target_path = None
    try:
        try:
            import httpx
        except ImportError:
            httpx = None

        info = _query_source_wheel_info(source_name, root_url)
        if not info:
            result_queue.put({"source": source_name, "complete": False, "stage": "metadata"})
            return

        source_directory = os.path.join(directory, subdirectory)
        os.makedirs(source_directory, exist_ok=True)
        target_path = os.path.join(source_directory, info["filename"])
        if httpx is not None:
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(None, connect=20)) as client:
                with client.stream("GET", info["url"]) as response:
                    response.raise_for_status()
                    with open(target_path, "wb") as file:
                        for chunk in response.iter_bytes(chunk_size=262144):
                            file.write(chunk)
        else:
            from urllib.request import Request, urlopen
            with urlopen(Request(info["url"], headers={"User-Agent": "pip/24.0"}), timeout=30) as response:
                with open(target_path, "wb") as file:
                    while chunk := response.read(262144):
                        file.write(chunk)

        size = os.path.getsize(target_path)
        if info.get("size") and size != info["size"]:
            raise RuntimeError(f"wheel size mismatch: expected {info['size']}, got {size}")
        if info.get("sha256"):
            digest = hashlib.sha256()
            with open(target_path, "rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest().lower() != info["sha256"].lower():
                raise RuntimeError("wheel SHA-256 verification failed")
        result_queue.put({"source": source_name, "path": target_path, "size": size, "complete": True})
    except Exception:
        try:
            if target_path and os.path.isfile(target_path):
                os.remove(target_path)
        except OSError:
            pass
        try:
            result_queue.put({"source": source_name, "complete": False})
        except Exception:
            pass


def _race_download_wheel(directory: str, sources: list[tuple[str, str]] | None = None, timeout_seconds: float = 300) -> dict | None:
    """Return the first complete, verified wheel; do not terminate healthy racers before a winner exists."""
    import multiprocessing

    sources = sources or PYMUPDF_DOWNLOAD_SOURCES
    logger.info("Starting multi-source PyMuPDF download race: %s", ", ".join(name for name, _ in sources))
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = {}
    for index, (source_name, root_url) in enumerate(sources):
        subdirectory = f"src{index}"
        process = context.Process(
            target=_source_download_process_main,
            args=(source_name, root_url, subdirectory, directory, result_queue),
            daemon=True,
        )
        process.start()
        processes[process] = (source_name, subdirectory)

    winner = None
    failures = 0
    started = time.monotonic()
    try:
        while winner is None and failures < len(processes):
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                logger.warning("Timed out after %s seconds waiting for a PyMuPDF download winner", timeout_seconds)
                break
            try:
                message = result_queue.get(timeout=min(remaining, 1.0))
            except Exception:
                if not any(process.is_alive() for process in processes):
                    break
                continue
            if message.get("complete"):
                winner = {"source": message["source"], "path": message["path"], "size": message["size"]}
                logger.info("Source %s finished downloading; terminating the other racers", message["source"])
                break
            failures += 1
    finally:
        for process, (name, _) in processes.items():
            if winner is not None and name == winner["source"]:
                continue
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
        for process, (name, _) in processes.items():
            try:
                process.join(timeout=3 if winner and name == winner["source"] else 1)
            except Exception:
                pass
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass
        for _, (name, subdirectory) in processes.items():
            if winner is not None and name == winner["source"]:
                continue
            shutil.rmtree(os.path.join(directory, subdirectory), ignore_errors=True)

    if winner is None:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return winner


def _format_size(byte_count: int) -> str:
    if byte_count >= 1_000_000:
        return f"{byte_count / 1_000_000:.2f} MB"
    if byte_count >= 1_000:
        return f"{byte_count / 1_000:.1f} KB"
    return f"{byte_count} B"


def _mark_pymupdf_ready():
    global _pymupdf_ready, _fitz
    try:
        _fitz = importlib.import_module("pymupdf")
    except ImportError:
        _fitz = importlib.import_module("fitz")
    _pymupdf_ready = True
    return _fitz


@contextmanager
def _pymupdf_install_lock(timeout_seconds: float = 900):
    """Hold a cross-process lock in the system temporary directory; the OS releases it if the owner crashes."""
    lock_path = os.path.join(tempfile.gettempdir(), "mcp-image-pymupdf-install.lock")
    with open(lock_path, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        started = time.monotonic()
        logged_wait = False
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError(f"Timed out after {timeout_seconds} seconds waiting for the PyMuPDF install lock: {lock_path}")
                if not logged_wait:
                    logger.info("Another process is installing PyMuPDF; waiting to reuse its installation")
                    logged_wait = True
                time.sleep(0.5)
        try:
            yield
        finally:
            try:
                lock_file.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _select_pymupdf_install_dir() -> str:
    """Use the user site-packages directory when purelib is not writable."""
    import site
    import sysconfig

    candidates = [sysconfig.get_path("purelib"), site.getusersitepackages()]
    for directory in candidates:
        if not directory:
            continue
        try:
            os.makedirs(directory, exist_ok=True)
            probe = os.path.join(directory, f".mcp-write-test-{os.getpid()}")
            with open(probe, "w"):
                pass
            os.remove(probe)
            return directory
        except OSError:
            continue
    return sysconfig.get_path("purelib")


def _install_local_wheel(wheel_path: str) -> bool:
    """Safely extract a compatible wheel while holding the install lock and reject archive path traversal."""
    import zipfile

    target_directory = _select_pymupdf_install_dir()
    logger.info("Installing PyMuPDF into %s", target_directory)
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative_path = PurePosixPath(member.filename)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise RuntimeError(f"wheel contains an unsafe path: {member.filename}")
                target_file = os.path.join(target_directory, *relative_path.parts)
                os.makedirs(os.path.dirname(target_file), exist_ok=True)
                with archive.open(member) as source, open(target_file, "wb") as target:
                    shutil.copyfileobj(source, target)
        importlib.invalidate_caches()
        if target_directory not in sys.path and os.path.isdir(target_directory):
            sys.path.insert(0, target_directory)
        logger.info("PyMuPDF installation completed")
        return True
    except Exception:
        logger.exception("Failed to extract and install PyMuPDF")
        return False


def ensure_pymupdf():
    """Install PyMuPDF on first PDF use, with thread and cross-process locks preventing duplicate installations."""
    global _pymupdf_ready
    if _pymupdf_ready:
        return _fitz
    with _pymupdf_thread_lock:
        if _pymupdf_ready:
            return _fitz
        try:
            return _mark_pymupdf_ready()
        except ImportError:
            pass
        with _pymupdf_install_lock():
            try:
                return _mark_pymupdf_ready()
            except ImportError:
                pass
            with tempfile.TemporaryDirectory(prefix="pymupdf-wheel-") as download_directory:
                winner = _race_download_wheel(download_directory)
                installed = False
                if winner:
                    logger.info("Using wheel from %s (%s)", winner["source"], _format_size(winner["size"]))
                    installed = _install_local_wheel(winner["path"])
            if not installed:
                raise RuntimeError("Every source failed to download or install PyMuPDF; you can also run `pip install pymupdf` manually and retry")
            try:
                return _mark_pymupdf_ready()
            except ImportError as error:
                import sysconfig
                raise ImportError(
                    f"PyMuPDF is still not importable after installation; interpreter: {sys.executable}; purelib: {sysconfig.get_path('purelib')}"
                ) from error


def _validate_pdf_path(pdf_path: str, max_file_bytes: int) -> Path:
    if not pdf_path:
        raise ValueError("PDF file path is required")
    path = Path(pdf_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"PDF file not found: {path}")
    size = path.stat().st_size
    if size > max_file_bytes:
        raise ValueError(f"PDF file is {size} bytes, exceeding the {max_file_bytes}-byte limit")
    return path


def get_pdf_page_count(pdf_path: str, max_file_bytes: int = 50 * 1024 * 1024) -> int:
    path = _validate_pdf_path(pdf_path, max_file_bytes)
    fitz = ensure_pymupdf()
    with fitz.open(path) as document:
        return int(document.page_count)


def render_pdf_page(
    pdf_path: str,
    page_number: int,
    dpi: int = 150,
    max_pixels: int = 40_000_000,
    max_file_bytes: int = 50 * 1024 * 1024,
) -> bytes:
    """Render one PDF page to PNG and validate its output dimensions before allocating pixels."""
    path = _validate_pdf_path(pdf_path, max_file_bytes)
    fitz = ensure_pymupdf()
    dpi = max(50, min(int(dpi), 1200))
    with fitz.open(path) as document:
        if page_number < 1 or page_number > document.page_count:
            raise ValueError(f"Page {page_number} is out of range; this PDF has {document.page_count} pages numbered from 1")
        page = document[page_number - 1]
        width = math.ceil(page.rect.width * dpi / 72)
        height = math.ceil(page.rect.height * dpi / 72)
        if width * height > max_pixels:
            raise ValueError(f"Page {page_number} is {width}x{height} at {dpi} DPI, exceeding the {max_pixels / 1_000_000:g}-megapixel limit")
        return page.get_pixmap(dpi=dpi, alpha=False).tobytes("png")
