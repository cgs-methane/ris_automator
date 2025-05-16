#!/usr/bin/env python3
"""RIS & Covidence Pipeline Tool
---------------------------------
A Streamlit app that provides three independent pipelines:

1. **PDF Downloader** – search Crossref by title/keywords or DOI and fetch the
   article PDF directly (publisher link first, Sci-Hub fallback).
2. **RIS Generator + Uploader** – query OpenAlex by title list, build RIS files
   and upload them to a Covidence review.
3. **PDF Extractor** – log into a Covidence review, crawl the *extracted* tab
   and bulk-download the linked PDFs.

The script includes an early monkey-patch so SciDownl can run on read-only
hosts (e.g. Streamlit Cloud) – its internal SQLite database is redirected to
``/tmp``.
"""

# ───────────────────── EARLY MONKEY-PATCH *MUST BE FIRST* ──────────────────────
from pathlib import Path  # noqa: E402 – top-level import allowed: does not
                          # touch scidownl yet, safe before the patch.

import os
import tempfile
import importlib
import functools
from sqlalchemy import create_engine

# Redirect SciDownl's default SQLite DB (which normally lives inside the
# site-packages directory and is therefore read-only on many cloud hosts)
# to a writable location in /tmp.
_tmp_db = os.path.join(tempfile.gettempdir(), "scidownl.db")

def _tmp_engine(echo: bool = False, test: bool = False):
    """Return a SQLAlchemy engine that points at our /tmp path."""
    return create_engine(f"sqlite:///{_tmp_db}?check_same_thread=False", echo=echo)

# Wrap importlib.import_module so that the very moment
# ``scidownl.db.entities`` is first imported we can overwrite its
# ``get_engine`` function *before* it calls ``create_tables()``.
_orig_import = importlib.import_module

@functools.wraps(_orig_import)
def _patched_import(name, *args, **kwargs):
    module = _orig_import(name, *args, **kwargs)
    if name == "scidownl.db.entities":
        # Swap the hard-coded get_engine with our patched version
        module.get_engine = _tmp_engine
    return module

importlib.import_module = _patched_import
# ───────────────────────────────────────────────────────────────────────────────

# SciDownl can now be imported safely (its DB will be created in /tmp)
from scidownl import scihub_download  # noqa: E402

# ─────────────────────────── Standard library imports ──────────────────────────
import re
import io
import csv
import time
import zipfile
import base64
import shutil
import requests

# ────────────────────────────── Third-party libs ───────────────────────────────
import streamlit as st
import streamlit.components.v1 as components
from rapidfuzz import fuzz

# Selenium stack
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager

# ─────────────────────────────── Configuration ────────────────────────────────
CROSSREF_API = "https://api.crossref.org/works"
SIM_THRESHOLD = 75
MAX_ROWS = 20

SCI_HUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ee",
    "https://sci-hub.ren",
]

# ──────────────────────────── Helper functions ────────────────────────────────
# Cross-ref helpers
@st.cache_data(show_spinner=False)
def crossref_by_title(q: str, rows: int = MAX_ROWS):
    r = requests.get(CROSSREF_API, params={"query": q, "rows": rows}, timeout=15)
    r.raise_for_status()
    return r.json()["message"]["items"]


@st.cache_data(show_spinner=False)
def crossref_by_doi(doi: str):
    r = requests.get(f"{CROSSREF_API}/{requests.utils.quote(doi)}", timeout=15)
    r.raise_for_status()
    return r.json()["message"]


def best_match(items, query: str):
    best_item, best_score = None, 0
    for it in items:
        title = it.get("title", [""])[0]
        score = fuzz.partial_ratio(query.lower(), title.lower())
        if score > best_score:
            best_item, best_score = it, score
    return best_item, best_score


# PDF helpers

def try_publisher_pdf(item):
    for link in item.get("link", []):
        if link.get("content-type") == "application/pdf":
            r = requests.get(link["URL"], timeout=20)
            if r.ok and r.headers.get("content-type", "").startswith("application/pdf"):
                return r.content
    return None


def fetch_via_scihub(doi: str, mirrors=SCI_HUB_MIRRORS):
    last_exc = None
    for mirror in mirrors:
        try:
            with tempfile.TemporaryDirectory() as td:
                out_fp = Path(td) / "paper.pdf"
                scihub_download(doi, paper_type="doi", out=str(out_fp), scihub_url=mirror)
                return out_fp.read_bytes()
        except Exception as exc:  # noqa: BLE001 (broad OK here)
            last_exc = exc
    raise RuntimeError(f"All mirrors failed; last error: {last_exc}")


def strip_doi(text: str) -> str:
    text = text.strip()
    if text.lower().startswith("http"):
        return re.sub(r"https?://(dx\.)?doi\.org/", "", text, flags=re.I)
    return text


# Misc helpers

def find_firefox_binary():
    for candidate in ("firefox-esr", "firefox"):
        p = shutil.which(candidate)
        if p:
            return p
    return None


def reconstruct_abstract(inv_idx):
    positions = [pos for idxs in inv_idx.values() for pos in idxs] if inv_idx else []
    if not positions:
        return ""
    words = [""] * (max(positions) + 1)
    for word, idxs in inv_idx.items():
        for i in idxs:
            words[i] = word
    return " ".join(words)


def create_ris_entry(title, authors, year, doi, abstract):
    lines = ["TY  - JOUR", f"TI  - {title}"]
    lines += [f"AU  - {au}" for au in authors]
    if year:
        lines.append(f"PY  - {year}")
    if doi:
        lines.append(f"DO  - {doi}")
    if abstract:
        lines.append(f"AB  - {abstract}")
    lines.append("ER  -")
    return "\n".join(lines)


def download_ris_for_article(title, out_dir, idx):
    OA = "https://api.openalex.org/works"
    try:
        rsp = requests.get(OA, params={"search": title}, timeout=15)
        rsp.raise_for_status()
    except requests.RequestException as e:
        st.error(f"OpenAlex search error for '{title}': {e}")
        return None

    results = rsp.json().get("results", [])
    if not results:
        st.warning(f"No results for '{title}'.")
        return None

    r0 = results[0]
    _title = r0.get("display_name", "No Title")
    doi  = r0.get("doi")
    year = r0.get("publication_year", "")
    authors = [a["author"]["display_name"] for a in r0.get("authorships", [])]
    abstract = reconstruct_abstract(r0.get("abstract_inverted_index", {}))

    st.info(f"Found: {_title} ({year})")
    ris_text = create_ris_entry(_title, authors, year, doi, abstract)
    path = Path(out_dir) / f"{idx}.ris"
    try:
        path.write_text(ris_text, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        st.error(f"Write error for '{_title}': {e}")
        return None
    return str(path) if path.stat().st_size else None


def download_all_ris_files(titles, out_dir):
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    files = [download_ris_for_article(t, out_dir, i) for i, t in enumerate(titles, 1)]
    return [f for f in files if f]


def upload_ris_files_to_covidence(ris_dir, email, password, review_url):
    fx_bin = find_firefox_binary()
    if not fx_bin:
        st.error("Firefox binary not found – install firefox-esr.")
        return

    opts = FirefoxOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.binary_location = fx_bin

    try:
        drv = webdriver.Firefox(service=FirefoxService(GeckoDriverManager().install()),
                                options=opts)
    except Exception as e:  # noqa: BLE001
        st.error(f"WebDriver init failed: {e}")
        return

    try:
        drv.get("https://app.covidence.org/sign_in")
        drv.maximize_window()
        time.sleep(3)
        drv.find_element(By.ID, "session_email").send_keys(email)
        drv.find_element(By.NAME, "session[password]").send_keys(password)
        drv.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]').click()
        time.sleep(5)

        for ris in sorted(Path(ris_dir).glob("*.ris")):
            st.write(f"Uploading {ris.name} …")
            drv.get(f"{review_url}/citation_imports/new")
            time.sleep(2)
            Select(drv.find_element(By.NAME, "citation_import[study_category]"))\
                .select_by_visible_text("Screen")
            drv.find_element(By.ID, "citation_import_file").send_keys(str(ris))
            drv.find_element(By.ID, "upload-citations").click()
            time.sleep(5)
    finally:
        drv.quit()
    st.success("RIS upload complete.")


# --- Helper utilities for PDF extraction pipeline -----------------------------

def sanitize_filename(name):
    sanitized = re.sub(r"[^\w\-\.\ ]+", "", name)
    return sanitized.strip().replace(" ", "_")


def trigger_download(data, filename, mime):
    b64 = base64.b64encode(data if isinstance(data, bytes) else data.encode()).decode()
    href = f"data:{mime};base64,{b64}"
    components.html(
        f"""
        <html><body>
            <a id='dl' href='{href}' download='{filename}'></a>
            <script>document.getElementById('dl').click();</script>
        </body></html>
        """,
        height=0,
        width=0,
    )


# (Functions download_pdf_from_study_element and extract_and_download_pdfs_from_covidence
# are unchanged from the user's original code – copied verbatim below.)

def download_pdf_from_study_element(driver, study_element, file_index):
    try:
        inner_load = study_element.find_element(By.XPATH, ".//button[contains(., 'Load more')]")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", inner_load)
        time.sleep(1)
        inner_load.click()
        st.write("Clicked inner 'Load more'.")
        time.sleep(2)
    except NoSuchElementException:
        pass

    try:
        view_btn = study_element.find_element(By.CSS_SELECTOR, "button.css-wetlpj")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", view_btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", view_btn)
        st.write("Clicked 'View full text'.")
        time.sleep(2)
    except Exception as e:  # noqa: BLE001
        st.error(f"View button error: {e}")
        return sanitize_filename(f"document_{file_index}"), None

    try:
        title = study_element.find_element(By.CSS_SELECTOR,
                                           "h2.webpack-concepts-Extraction-StudyList-StudyReference-module__title").text.strip()
    except Exception:  # noqa: BLE001
        title = f"document_{file_index}"
    title = title or f"document_{file_index}"

    try:
        pdf_link = study_element.find_element(By.CSS_SELECTOR,
                            "li.webpack-concepts-Extraction-StudyList-Documents-module__documentContainer a").get_attribute("href")
    except Exception as e:  # noqa: BLE001
        st.error(f"PDF link not found: {e}")
        return sanitize_filename(title), None

    try:
        pdf_bytes = requests.get(pdf_link, timeout=20).content
    except Exception as e:  # noqa: BLE001
        st.error(f"PDF download error: {e}")
        return sanitize_filename(title), None

    st.success(f"Downloaded '{title}'.")
    return sanitize_filename(title), pdf_bytes


def extract_and_download_pdfs_from_covidence(email, password, review_url):
    fx_bin = find_firefox_binary()
    if not fx_bin:
        st.error("Firefox binary not found – install firefox-esr.")
        return {}, []

    opts = FirefoxOptions()
    # opts.add_argument("--headless")  # optional
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.binary_location = fx_bin

    try:
        drv = webdriver.Firefox(service=FirefoxService(GeckoDriverManager().install()),
                                options=opts)
    except Exception as e:  # noqa: BLE001
        st.error(f"WebDriver init failed: {e}")
        return {}, []

    downloaded, failed = {}, []
    try:
        drv.get("https://app.covidence.org/sign_in")
        drv.maximize_window()
        time.sleep(3)
        drv.find_element(By.ID, "session_email").send_keys(email)
        drv.find_element(By.NAME, "session[password]").send_keys(password)
        drv.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]').click()
        time.sleep(5)
        drv.get(review_url)
        time.sleep(3)
        try:
            drv.find_element(By.PARTIAL_LINK_TEXT, "extracted").click()
            st.info("Opened 'extracted' studies.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Extracted link error: {e}")
            return downloaded, failed
        time.sleep(3)

        while True:
            try:
                load_more = drv.find_element(By.XPATH, "//button[contains(., 'Load more')]")
                drv.execute_script("arguments[0].scrollIntoView({block: 'center'});", load_more)
                time.sleep(1)
                load_more.click()
                st.write("Clicked global 'Load more'.")
                time.sleep(3)
            except NoSuchElementException:
                break

        studies = drv.find_elements(By.CSS_SELECTOR, "article[class*='StudyListItem']")
        for i, study in enumerate(studies, 1):
            st.write(f"Processing study {i} …")
            fname, pdf = download_pdf_from_study_element(drv, study, i)
            if pdf:
                downloaded[f"{fname}.pdf"] = pdf
            else:
                failed.append(f"{fname}.pdf")
    finally:
        drv.quit()
    return downloaded, failed

# ────────────────────────────── Streamlit UI ────────────────────────────────

def main():
    st.set_page_config(page_title="Pipeline Tool", page_icon="📄")
    st.header("CGS- Paper, RIS & Covidence Pipeline Tool")
    st.markdown(
        """
        **Pipelines**
        1. *PDF Downloader*  
        2. *RIS ↓ / Covidence ↑*  
        3. *PDF Extractor*
        """
    )

    p1, p2, p3 = st.tabs(["PDF Downloader", "RIS Files", "PDF Extraction"])

    # ---------------- Pipeline 1 ----------------
    with p1:
        st.header("PDF Downloader for any Article using AI Agent")
        mode = st.radio("Search by", ["Title / keywords", "DOI"], horizontal=True)

        if mode == "Title / keywords":
            query = st.text_input("Enter title or keywords")
            if st.button("Search & Download") and query.strip():
                with st.spinner("Searching Crossref …"):
                    items = crossref_by_title(query)
                if not items:
                    st.error("No results.")
                    return
                itm, score = best_match(items, query)
                if score < SIM_THRESHOLD:
                    st.warning(f"Best match only {score}%. Provide a more precise title.")
                    return
                title, doi = itm["title"][0], itm["DOI"]
                st.info(f"*{title}*  \nDOI: `{doi}`  · {score}% similarity")
                with st.spinner("Fetching PDF …"):
                    pdf = try_publisher_pdf(itm) or fetch_via_scihub(doi)
                if pdf:
                    st.success("PDF ready!")
                    st.download_button("📥 Save", pdf, file_name=doi.replace("/", "_") + ".pdf", mime="application/pdf")

        else:
            doi_in = st.text_input("Enter DOI or https://doi.org/…")
            if st.button("Download PDF") and doi_in.strip():
                doi = strip_doi(doi_in)
                try:
                    itm = crossref_by_doi(doi)
                except Exception as e:  # noqa: BLE001
                    st.error(f"Crossref error: {e}")
                    return
                title = itm.get("title", [""])[0]
                st.info(f"*{title}*")
                with st.spinner("Fetching PDF …"):
                    pdf = try_publisher_pdf(itm) or fetch_via_scihub(doi)
                if pdf:
                    st.success("PDF ready!")
                    st.download_button("📥 Save", pdf, file_name=doi.replace("/", "_") + ".pdf", mime="application/pdf")

    # ---------------- Pipeline 2 ----------------
    with p2:
        st.header("RIS Generator ➜ Covidence Uploader")
        titles_txt = st.text_area("Article titles (one per line)")
        email  = st.text_input("Covidence email")
        pwd    = st.text_input("Covidence password", type="password")
        rv_url = st.text_input("Review URL", value="https://app.covidence.org/reviews/…")
        if st.button("Run RIS Pipeline"):
            titles = [t.strip() for t in titles_txt.splitlines() if t.strip()]
            if not (titles and email and pwd and rv_url):
                st.error("Please fill all fields.")
                return
            ris_dir = Path.cwd() / "RIS_files"
            st.subheader("Step 1 — Download RIS")
            files = download_all_ris_files(titles, ris_dir)
            if not files:
                st.error("No RIS files were created.")
                return
            st.subheader("Step 2 — Upload to Covidence")
            upload_ris_files_to_covidence(ris_dir, email, pwd, rv_url)

    # ---------------- Pipeline 3 ----------------
    with p3:
        st.header("PDF Extraction from Covidence")
        email  = st.text_input("Covidence email", key="p3_email")
        pwd    = st.text_input("Covidence password", type="password", key="p3_pwd")
        rv_url = st.text_input("Review URL", value="https://app.covidence.org/reviews/…", key="p3_url")
        if st.button("Run PDF Extraction"):
            if not (email and pwd and rv_url):
                st.error("Please fill all fields.")
                return
            with st.spinner("Extracting PDFs …"):
                pdfs, fails = extract_and_download_pdfs_from_covidence(email, pwd, rv_url)
            if pdfs:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fn, data in pdfs.items():
                        zf.writestr(fn, data)
                buf.seek(0)
                trigger_download(buf.getvalue(), "downloaded_pdfs.zip", "application/zip")
            if fails:
                st.warning(f"{len(fails)} papers failed. CSV will download …")
                csv_buf = io.StringIO()
                csv.writer(csv_buf).writerow(["Paper Title"])
                csv.writer(csv_buf).writerows([[f] for f in fails])
                trigger_download(csv_buf.getvalue(), "failed_papers.csv", "text/csv")


if __name__ == "__main__":
    main()

# -------------------------- Installation hint --------------------------
# pip install streamlit requests rapidfuzz scidownl sqlalchemy selenium
#             webdriver-manager
