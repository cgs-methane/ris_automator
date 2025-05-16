#!/usr/bin/env python3
# Merge_ris+cov.py  –  Streamlit “RIS & Covidence” helper
# ------------------------------------------------------------------------------
# Fix for Streamlit Cloud: redirect SciDownl’s SQLite file to a writable folder
# BEFORE SciDownl (or anything that imports it) is loaded.
# ------------------------------------------------------------------------------

# ───────────────────────── PATCH MUST BE ABSOLUTE FIRST ─────────────────────────
import os, tempfile, sqlalchemy                   # pylint: disable=wrong-import-position

_tmp_db = os.path.join(tempfile.gettempdir(), "scidownl.db")
_real_create_engine = sqlalchemy.create_engine    # keep original ref


def _patched_create_engine(url, *args, **kwargs):  # noqa: D401 (simple function)
    """
    If SciDownl tries to create its default engine (SQLite file living inside
    site-packages/scidownl/…), rewrite the URL so the DB lands in /tmp instead.
    All other SQLAlchemy usages pass through untouched.
    """
    if isinstance(url, str) and url.startswith("sqlite:///") and "scidownl" in url:
        url = f"sqlite:///{_tmp_db}?check_same_thread=False"
    return _real_create_engine(url, *args, **kwargs)


sqlalchemy.create_engine = _patched_create_engine
# ────────────────────────────────────────────────────────────────────────────────


# ------------------------------------------------------------------------------
# ❶  Standard library imports that do NOT trigger SciDownl
# ------------------------------------------------------------------------------
from pathlib import Path
import re, io, csv, time, zipfile, base64, shutil, tempfile, requests

# ------------------------------------------------------------------------------
# ❷  NOW it is safe to import SciDownl (and everything that depends on it)
# ------------------------------------------------------------------------------
from scidownl import scihub_download   # noqa: E402  (imported late on purpose)

# ------------------------------------------------------------------------------
# ❸  Third-party and Streamlit imports
# ------------------------------------------------------------------------------
import streamlit as st
import streamlit.components.v1 as components
from rapidfuzz import fuzz

# Selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager

# ------------------------------------------------------------------------------
# ❹  Constants
# ------------------------------------------------------------------------------
CROSSREF_API  = "https://api.crossref.org/works"
SIM_THRESHOLD = 75
MAX_ROWS      = 20
SCI_HUB_MIRRORS = [
    "https://sci-hub.se", "https://sci-hub.st", "https://sci-hub.ru",
    "https://sci-hub.ee", "https://sci-hub.ren"
]

# ------------------------------------------------------------------------------
# ❺  Cross-ref helpers
# ------------------------------------------------------------------------------
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


# ------------------------------------------------------------------------------
# ❻  PDF helpers
# ------------------------------------------------------------------------------
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
        except Exception as exc:             # noqa: BLE001  (broad but OK here)
            last_exc = exc
    raise RuntimeError(f"All mirrors failed; last error: {last_exc}")


def strip_doi(text: str) -> str:
    text = text.strip()
    if text.lower().startswith("http"):
        return re.sub(r"https?://(dx\.)?doi\.org/", "", text, flags=re.I)
    return text


# ------------------------------------------------------------------------------
# ❼  Helpers for Pipelines 1 & 2
# ------------------------------------------------------------------------------
def find_firefox_binary():
    for bn in ("firefox-esr", "firefox"):
        p = shutil.which(bn)
        if p:
            return p
    return None


def reconstruct_abstract(inv_idx):
    words = [""] * (max(m for idxs in inv_idx.values() for m in idxs) + 1) if inv_idx else []
    for w, idxs in inv_idx.items():
        for i in idxs:
            words[i] = w
    return " ".join(words).strip()


def create_ris_entry(title, authors, year, doi, abstract):
    lines = ["TY  - JOUR", f"TI  - {title}"]
    lines += [f"AU  - {a}" for a in authors]
    if year:     lines.append(f"PY  - {year}")
    if doi:      lines.append(f"DO  - {doi}")
    if abstract: lines.append(f"AB  - {abstract}")
    lines.append("ER  -")
    return "\n".join(lines)


def download_ris_for_article(article_title, output_folder, file_index):
    OA_API = "https://api.openalex.org/works"
    try:
        resp = requests.get(OA_API, params={"search": article_title}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        st.error(f"OpenAlex search error for '{article_title}': {e}")
        return None

    results = resp.json().get("results", [])
    if not results:
        st.warning(f"No results for '{article_title}'.")
        return None

    r0      = results[0]
    title   = r0.get("display_name", "No Title")
    doi     = r0.get("doi")
    year    = r0.get("publication_year", "")
    authors = [au["author"]["display_name"] for au in r0.get("authorships", [])]

    abstract = reconstruct_abstract(r0.get("abstract_inverted_index", {}))
    st.info(f"Found article: {title} ({year})")

    ris_path = Path(output_folder) / f"{file_index}.ris"
    try:
        ris_path.write_text(create_ris_entry(title, authors, year, doi, abstract), encoding="utf-8")
        st.success(f"Saved {ris_path.name}")
    except Exception as e:                  # noqa: BLE001
        st.error(f"Writing RIS failed for '{article_title}': {e}")
        return None
    return str(ris_path) if ris_path.stat().st_size else None


def download_all_ris_files(titles, out_dir):
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    files = [download_ris_for_article(t, out_dir, i) for i, t in enumerate(titles, 1)]
    return [f for f in files if f]


# ------------------------------------------------------------------------------
# ❽  Covidence automation (upload / extract)
# ------------------------------------------------------------------------------
def upload_ris_files_to_covidence(ris_folder, email, password, review_url):
    fx_bin = find_firefox_binary()
    if not fx_bin:
        st.error("Firefox binary not found. Install firefox-esr.")
        return

    opts = FirefoxOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.binary_location = fx_bin

    try:
        driver = webdriver.Firefox(
            service=FirefoxService(executable_path=GeckoDriverManager().install()),
            options=opts,
        )
    except Exception as e:                                      # noqa: BLE001
        st.error(f"WebDriver init error: {e}")
        return

    try:
        driver.get("https://app.covidence.org/sign_in")
        driver.maximize_window()
        time.sleep(3)
        driver.find_element(By.ID, 'session_email').send_keys(email)
        driver.find_element(By.NAME, 'session[password]').send_keys(password)
        driver.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]').click()
        time.sleep(5)

        for ris in sorted(Path(ris_folder).glob("*.ris")):
            st.write(f"Uploading {ris.name} …")
            driver.get(f"{review_url}/citation_imports/new")
            time.sleep(2)
            Select(driver.find_element(By.NAME, 'citation_import[study_category]')).select_by_visible_text('Screen')
            driver.find_element(By.ID, 'citation_import_file').send_keys(str(ris))
            driver.find_element(By.ID, 'upload-citations').click()
            time.sleep(5)
    finally:
        driver.quit()
    st.success("All RIS files processed.")


# (The rest of the Selenium PDF-extraction functions remain unchanged – omitted
# here for brevity but copy from your current version.)
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# ❾  Streamlit UI
# ------------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Pipeline Tool", page_icon="📄")
    st.title("RIS & Covidence Pipeline Tool")

    tab1, tab2, tab3 = st.tabs([
        "Pipeline 1 – PDF Downloader",
        "Pipeline 2 – RIS Files",
        "Pipeline 3 – PDF Extraction"
    ])

    # ------------------------- Pipeline 1 -------------------------
    with tab1:
        st.header("One-Click PDF Downloader")
        mode = st.radio("I have a …", ["Title / keywords", "DOI"], horizontal=True)

        if mode == "Title / keywords":
            query = st.text_input("Enter title or keywords")
            if st.button("Search & Download") and query.strip():
                with st.spinner("Searching Crossref …"):
                    items = crossref_by_title(query)
                if not items:
                    st.error("No Crossref results.")
                    return
                itm, score = best_match(items, query)
                if score < SIM_THRESHOLD:
                    st.warning(f"Best match {score}% < threshold {SIM_THRESHOLD}%.")
                    return

                title, doi = itm["title"][0], itm["DOI"]
                st.info(f"*{title}*  \nDOI: `{doi}`  · similarity {score}%")

                with st.spinner("Fetching PDF …"):
                    pdf = try_publisher_pdf(itm) or fetch_via_scihub(doi)
                if pdf:
                    st.success("PDF ready!")
                    st.download_button("📥 Save PDF", pdf,
                                       file_name=doi.replace("/", "_") + ".pdf",
                                       mime="application/pdf")
        else:
            doi_in = st.text_input("Enter DOI or https://doi.org/…")
            if st.button("Download PDF") and doi_in.strip():
                doi = strip_doi(doi_in)
                try:
                    itm = crossref_by_doi(doi)
                except Exception as e:                      # noqa: BLE001
                    st.error(f"Crossref lookup failed: {e}")
                    return
                title = itm.get("title", [""])[0]
                st.info(f"*{title}*")
                with st.spinner("Fetching PDF …"):
                    pdf = try_publisher_pdf(itm) or fetch_via_scihub(doi)
                if pdf:
                    st.success("PDF ready!")
                    st.download_button("📥 Save PDF", pdf,
                                       file_name=doi.replace("/", "_") + ".pdf",
                                       mime="application/pdf")

    # ------------------------- Pipeline 2 -------------------------
    with tab2:
        st.header("RIS ↓ / Covidence ↑")
        titles_txt = st.text_area("Article titles (one per line)")
        email = st.text_input("Covidence email")
        pwd   = st.text_input("Covidence password", type="password")
        rvurl = st.text_input("Review URL", value="https://app.covidence.org/reviews/…")

        if st.button("Run RIS Pipeline"):
            titles = [t.strip() for t in titles_txt.splitlines() if t.strip()]
            if not (titles and email and pwd and rvurl):
                st.error("Please fill all fields.")
                return
            RIS_DIR = Path.cwd() / "RIS_files"
            st.subheader("Step 1 – Downloading RIS")
            files = download_all_ris_files(titles, RIS_DIR)
            if not files:
                st.error("No RIS files created.")
                return
            st.subheader("Step 2 – Uploading to Covidence")
            upload_ris_files_to_covidence(RIS_DIR, email, pwd, rvurl)

    # ------------------------- Pipeline 3 -------------------------
    with tab3:
        st.header("PDF Extraction from Covidence")
        st.info("PDF-extraction functions unchanged — copy from your previous version.")

# ------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
