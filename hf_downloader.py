#!/usr/bin/env python3
"""
hf_downloader.py — скачивание файлов по прямой ссылке (Hugging Face и не только)

Умеет:
  - принимать как "resolve"-ссылки, так и "blob"-ссылки с Hugging Face
    (blob автоматически конвертируется в resolve)
  - докачивать оборванные загрузки (Range-запросы), если сервер это поддерживает
  - показывать прогресс-бар со скоростью и ETA
  - скачивать несколько файлов за один запуск
  - повторять попытку при обрыве соединения

Использование:
  python hf_downloader.py <ссылка> [ещё ссылки...]
  python hf_downloader.py <ссылка> -o имя_файла.apk
  python hf_downloader.py <ссылка> -d ./downloads
  python hf_downloader.py -f links.txt          # ссылки из файла, по одной на строку

Примеры:
  python hf_downloader.py https://huggingface.co/Zerchi/test/resolve/main/StandRise1.4.0f1.apk
  python hf_downloader.py "https://huggingface.co/Zerchi/dlrcn/blob/main/Date%20a%20live%20rio%20reincorntaion%20mobile/jp.ideafactory.datealive.apk"
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from tqdm import tqdm

CHUNK_SIZE = 1024 * 1024  # 1 МБ
MAX_RETRIES = 5
RETRY_DELAY = 3  # секунды
TIMEOUT = 30  # секунд на установление соединения/чтение чанка

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hf_downloader/1.0"
}


def normalize_hf_url(url: str) -> str:
    """
    Превращает ссылку вида .../blob/... в .../resolve/... (прямую ссылку на файл).
    Остальные ссылки (resolve, не-huggingface и т.п.) возвращает без изменений.
    """
    parsed = urllib.parse.urlparse(url)
    if "huggingface.co" in parsed.netloc and "/blob/" in parsed.path:
        new_path = parsed.path.replace("/blob/", "/resolve/", 1)
        parsed = parsed._replace(path=new_path)
        return urllib.parse.urlunparse(parsed)
    return url


def guess_filename(url: str) -> str:
    """Достаёт имя файла из URL (декодируя %20 и т.п.)."""
    parsed = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(parsed.path)).name
    return name or "downloaded_file"


def human_size(num_bytes: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} ПБ"


def download_file(url: str, dest_dir: Path, filename: str | None = None) -> bool:
    """
    Скачивает один файл с поддержкой докачки и повторных попыток.
    Возвращает True при успехе, False при провале.
    """
    url = normalize_hf_url(url)
    filename = filename or guess_filename(url)
    dest_path = dest_dir / filename
    tmp_path = dest_dir / (filename + ".part")

    dest_dir.mkdir(parents=True, exist_ok=True)

    if dest_path.exists():
        print(f"[=] Уже существует, пропускаю: {dest_path}")
        return True

    resume_pos = tmp_path.stat().st_size if tmp_path.exists() else 0

    for attempt in range(1, MAX_RETRIES + 1):
        headers = dict(HEADERS)
        if resume_pos:
            headers["Range"] = f"bytes={resume_pos}-"

        try:
            with requests.get(
                url, headers=headers, stream=True, timeout=TIMEOUT, allow_redirects=True
            ) as resp:
                # Если сервер не поддержал Range (вернул 200 вместо 206) — начинаем заново
                if resume_pos and resp.status_code == 200:
                    resume_pos = 0
                    tmp_path.unlink(missing_ok=True)
                elif resume_pos and resp.status_code == 416:
                    # "Range not satisfiable" — похоже, файл уже докачан полностью
                    tmp_path.rename(dest_path)
                    print(f"[✓] Готово (уже докачан): {dest_path}")
                    return True

                resp.raise_for_status()

                total_header = resp.headers.get("Content-Length")
                total = int(total_header) + resume_pos if total_header else None

                mode = "ab" if resume_pos else "wb"
                desc = filename if len(filename) <= 40 else filename[:37] + "..."

                with open(tmp_path, mode) as f, tqdm(
                    total=total,
                    initial=resume_pos,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=desc,
                ) as bar:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        bar.update(len(chunk))
                        resume_pos += len(chunk)

            tmp_path.rename(dest_path)
            print(f"[✓] Скачано: {dest_path} ({human_size(dest_path.stat().st_size)})")
            return True

        except (requests.exceptions.RequestException, OSError) as e:
            print(f"[!] Попытка {attempt}/{MAX_RETRIES} неудачна для {filename}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                resume_pos = tmp_path.stat().st_size if tmp_path.exists() else 0
            else:
                print(f"[x] Не удалось скачать: {url}")
                return False

    return False


def main():
    parser = argparse.ArgumentParser(
        description="Скачивание файлов по прямой ссылке (Hugging Face и др.)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("urls", nargs="*", help="Ссылки на файлы")
    parser.add_argument("-f", "--file", help="Файл со списком ссылок (по одной на строку)")
    parser.add_argument(
        "-d", "--dir", default="downloads", help="Папка для сохранения (по умолчанию: ./downloads)"
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Имя файла для сохранения (только если передана одна ссылка)",
    )
    args = parser.parse_args()

    urls: list[str] = list(args.urls)

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[x] Файл со ссылками не найден: {file_path}")
            sys.exit(1)
        urls.extend(
            line.strip()
            for line in file_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    if not urls:
        parser.print_help()
        sys.exit(1)

    if args.output and len(urls) > 1:
        print("[x] Флаг -o/--output можно использовать только с одной ссылкой")
        sys.exit(1)

    dest_dir = Path(args.dir)

    results = []
    for url in urls:
        ok = download_file(url, dest_dir, filename=args.output)
        results.append((url, ok))

    print("\n--- Итог ---")
    for url, ok in results:
        status = "OK" if ok else "ОШИБКА"
        print(f"[{status}] {url}")

    if any(not ok for _, ok in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
