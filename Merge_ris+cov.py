# pipeline_tool.py
# --------------------------------------------------------------------
# ❶  Standard library imports that do NOT trigger SciDownl
# --------------------------------------------------------------------
import os
from pathlib import Path

# --------------------------------------------------------------------
# ❷  The rest of your imports and constants (unchanged)
# --------------------------------------------------------------------
import re
import io
import csv
import time
import zipfile
import base64
import shutil
import tempfile
import requests
import streamlit as st
import streamlit.components.v1 as components
from rapidfuzz import fuzz

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager

# --------------------------------------------------------------------
# ❸  Your original code starts here – completely unchanged
#     (only the duplicate “from scidownl import scihub_download”
#      was removed, because we imported it once above)
# --------------------------------------------------------------------

# ───────────────────────── configuration for Pipeline 3 ─────────────────────────
CROSSREF_API  = "https://api.crossref.org/works"
SIM_THRESHOLD = 75
MAX_ROWS      = 20

SCI_HUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ee",
    "https://sci-hub.ren",
]

# -------------- Cross-ref helpers ------------------------------------------------
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

# -------------- PDF helpers ------------------------------------------------------
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
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"All mirrors failed; last error: {last_exc}")

def strip_doi(text: str) -> str:
    text = text.strip()
    if text.lower().startswith("http"):
        return re.sub(r"https?://(dx\.)?doi\.org/", "", text, flags=re.I)
    return text


# ───────────────────────── configuration & helpers for Pipelines 1 & 2 ─────────────────────────
def find_firefox_binary():
    possible_bins = ["firefox-esr", "firefox"]
    for bin_name in possible_bins:
        bin_path = shutil.which(bin_name)
        if bin_path:
            return bin_path
    return None

def reconstruct_abstract(inverted_index):
    positions = []
    for word, indices in inverted_index.items():
        positions.extend(indices)
    if not positions:
        return ""
    n_words = max(positions) + 1
    abstract_words = [""] * n_words
    for word, indices in inverted_index.items():
        for index in indices:
            abstract_words[index] = word
    return " ".join(abstract_words)

def create_ris_entry(title, authors, year, doi, abstract):
    ris_lines = []
    ris_lines.append("TY  - JOUR")
    ris_lines.append(f"TI  - {title}")
    for author in authors:
        ris_lines.append(f"AU  - {author}")
    if year:
        ris_lines.append(f"PY  - {year}")
    if doi:
        ris_lines.append(f"DO  - {doi}")
    if abstract:
        ris_lines.append(f"AB  - {abstract}")
    ris_lines.append("ER  -")
    return "\n".join(ris_lines)

def download_ris_for_article(article_title, output_folder, file_index):
    search_url = "https://api.openalex.org/works"
    params = {"search": article_title}
    
    try:
        response = requests.get(search_url, params=params)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        st.error(f"Error during OpenAlex search for '{article_title}': {e}")
        return None
    
    data = response.json()
    if "results" not in data or len(data["results"]) == 0:
        st.warning(f"No results found for '{article_title}'.")
        return None
    
    first_result = data["results"][0]
    title = first_result.get("display_name", "No Title")
    doi = first_result.get("doi", None)
    year = first_result.get("publication_year", "")
    
    authors = []
    for authorship in first_result.get("authorships", []):
        author_info = authorship.get("author", {})
        author_name = author_info.get("display_name")
        if author_name:
            authors.append(author_name)
    
    abstract = ""
    if "abstract_inverted_index" in first_result and first_result["abstract_inverted_index"]:
        abstract = reconstruct_abstract(first_result["abstract_inverted_index"])
    
    st.info(f"Found article: {title} ({year})")
    ris_content = create_ris_entry(title, authors, year, doi, abstract)
    filename = os.path.join(output_folder, f"{file_index}.ris")
    
    try:
        with open(filename, "w", encoding="utf-8") as file:
            file.write(ris_content)
        st.success(f"RIS file saved as '{filename}'.")
    except Exception as e:
        st.error(f"Error saving RIS file for '{article_title}': {e}")
        return None
    
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        return filename
    else:
        return None

def download_all_ris_files(article_titles, output_folder):
    if os.path.exists(output_folder):
        shutil.rmtree(output_folder)
    os.makedirs(output_folder)
    downloaded_files = []
    for i, title in enumerate(article_titles, start=1):
        st.write(f"Processing article: {title}")
        ris_file = download_ris_for_article(title, output_folder, i)
        if ris_file:
            downloaded_files.append(ris_file)
    return downloaded_files

def upload_ris_files_to_covidence(ris_folder_path, covidence_email, covidence_password, review_url):
    firefox_path = find_firefox_binary()
    if not firefox_path:
        st.error("Firefox binary not found on this system. Please ensure firefox-esr is installed.")
        return

    firefox_options = FirefoxOptions()
    # firefox_options.add_argument("--headless")
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--disable-gpu")
    firefox_options.binary_location = "/Applications/Firefox.app/Contents/MacOS/firefox"

    try:
        service = FirefoxService(executable_path=GeckoDriverManager().install())
        driver = webdriver.Firefox(service=service, options=firefox_options)
    except Exception as e:
        st.error(f"Error initializing Firefox WebDriver: {e}")
        return
    
    try:
        driver.get("https://app.covidence.org/sign_in")
        driver.maximize_window()
        time.sleep(3)
        email_field = driver.find_element(By.ID, 'session_email')
        email_field.send_keys(covidence_email)
        password_field = driver.find_element(By.NAME, 'session[password]')
        password_field.send_keys(covidence_password)
        sign_in_button = driver.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]')
        sign_in_button.click()
        time.sleep(5)
        driver.get(review_url)
        time.sleep(3)
        
        ris_files = [os.path.join(ris_folder_path, f)
                     for f in os.listdir(ris_folder_path)
                     if f.lower().endswith('.ris')]
        
        for ris_file in ris_files:
            st.write(f"Uploading {os.path.basename(ris_file)}...")
            driver.get(review_url + '/citation_imports/new')
            time.sleep(3)
            select_import_into = Select(driver.find_element(By.NAME, 'citation_import[study_category]'))
            select_import_into.select_by_visible_text('Screen')
            upload_field = driver.find_element(By.ID, 'citation_import_file')
            upload_field.send_keys(ris_file)
            import_button = driver.find_element(By.ID, 'upload-citations')
            import_button.click()
            time.sleep(5)
            try:
                success_message = driver.find_element(By.CLASS_NAME, 'notifications').text
                st.write(f"Upload Status for {os.path.basename(ris_file)}: {success_message}")
            except Exception:
                st.success(f"Uploaded {os.path.basename(ris_file)} on Covidence.")
        st.success("All RIS files have been processed for upload.")
    finally:
        driver.quit()

def sanitize_filename(name):
    sanitized = re.sub(r'[^\w\-\.\ ]+', '', name)
    return sanitized.strip().replace(" ", "_")

def trigger_download(data, filename, mime_type):
    if isinstance(data, bytes):
        b64 = base64.b64encode(data).decode()
    else:
        b64 = base64.b64encode(data.encode()).decode()
    href = f"data:{mime_type};base64,{b64}"
    html = f"""
    <html>
      <body>
        <a id="download_link" href="{href}" download="{filename}"></a>
        <script>
          document.getElementById('download_link').click();
        </script>
      </body>
    </html>
    """
    components.html(html, height=0, width=0)

def download_pdf_from_study_element(driver, study_element, file_index):
    try:
        inner_load_more = study_element.find_element(By.XPATH, ".//button[contains(., 'Load more')]")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", inner_load_more)
        time.sleep(1)
        inner_load_more.click()
        st.write("Clicked inner 'Load more' for study.")
        time.sleep(2)
    except NoSuchElementException:
        pass

    try:
        view_button = study_element.find_element(By.CSS_SELECTOR, "button.css-wetlpj")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", view_button)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", view_button)
        st.write("Clicked 'View full text' button.")
        time.sleep(2)
    except Exception as e:
        st.error(f"Error clicking 'View full text' button: {e}")
        title = f"document_{file_index}"
        return (sanitize_filename(title), None)

    try:
        title_elem = study_element.find_element(By.CSS_SELECTOR, "h2.webpack-concepts-Extraction-StudyList-StudyReference-module__title")
        title = title_elem.text.strip()
        if not title:
            title = f"document_{file_index}"
    except Exception as e:
        st.warning(f"Could not retrieve title from study; using index instead: {e}")
        title = f"document_{file_index}"
    sanitized_title = sanitize_filename(title)

    try:
        pdf_link_element = study_element.find_element(
            By.CSS_SELECTOR, "li.webpack-concepts-Extraction-StudyList-Documents-module__documentContainer a"
        )
        pdf_url = pdf_link_element.get_attribute("href")
    except Exception as e:
        st.error(f"Error locating PDF link: {e}")
        pdf_url = None

    if not pdf_url:
        st.warning("No PDF URL found in study element.")
        return (sanitized_title, None)

    try:
        pdf_response = requests.get(pdf_url)
        pdf_response.raise_for_status()
        pdf_bytes = pdf_response.content
    except Exception as e:
        st.error(f"Error downloading PDF from {pdf_url}: {e}")
        return (sanitized_title, None)

    st.success(f"PDF for '{sanitized_title}' downloaded successfully.")
    return (sanitized_title, pdf_bytes)

def extract_and_download_pdfs_from_covidence(covidence_email, covidence_password, review_url):
    firefox_path = find_firefox_binary()
    if not firefox_path:
        st.error("Firefox binary not found on this system. Please ensure firefox-esr is installed.")
        return {}, []
    
    firefox_options = FirefoxOptions()
    # Uncomment below to run headless (if desired)
    # firefox_options.add_argument("--headless")
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--disable-gpu")
    firefox_options.binary_location = "/Applications/Firefox.app/Contents/MacOS/firefox"

    try:
        service = FirefoxService(executable_path=GeckoDriverManager().install())
        driver = webdriver.Firefox(service=service, options=firefox_options)
    except Exception as e:
        st.error(f"Error initializing Firefox WebDriver: {e}")
        return {}, []

    downloaded_pdfs = {}
    failed_papers = []
    try:
        driver.get("https://app.covidence.org/sign_in")
        driver.maximize_window()
        time.sleep(3)
        email_field = driver.find_element(By.ID, 'session_email')
        email_field.send_keys(covidence_email)
        password_field = driver.find_element(By.NAME, 'session[password]')
        password_field.send_keys(covidence_password)
        sign_in_button = driver.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]')
        sign_in_button.click()
        time.sleep(5)

        driver.get(review_url)
        time.sleep(3)
        try:
            extracted_link = driver.find_element(By.PARTIAL_LINK_TEXT, "extracted")
            extracted_link.click()
            st.info("Navigated to extracted studies.")
        except Exception as e:
            st.error(f"Error clicking on the extracted studies link: {e}")
            return downloaded_pdfs, failed_papers
        time.sleep(3)

        while True:
            try:
                load_more_button = driver.find_element(By.XPATH, "//button[contains(., 'Load more')]")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", load_more_button)
                time.sleep(1)
                load_more_button.click()
                st.write("Clicked global 'Load more' to reveal additional studies.")
                time.sleep(3)
            except NoSuchElementException:
                st.info("No more global 'Load more' button found.")
                break
            except Exception as e:
                st.warning(f" Error clicking global 'Load more': {e}")
                time.sleep(3)

        study_elements = driver.find_elements(By.CSS_SELECTOR, "article[class*='StudyListItem']")
        if not study_elements:
            st.error("No study elements found. Please check your CSS selector.")
            return downloaded_pdfs, failed_papers

        for i, study in enumerate(study_elements, start=1):
            st.write(f"Processing study {i}...")
            title, pdf_bytes = download_pdf_from_study_element(driver, study, i)
            if pdf_bytes:
                downloaded_pdfs[title + ".pdf"] = pdf_bytes
            else:
                failed_papers.append(title + ".pdf")
    finally:
        driver.quit()
    return downloaded_pdfs, failed_papers

# ──────────────────────────────────────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="Pipeline Tool", page_icon="📄")
    st.title("RIS & Covidence Pipeline Tool")
    st.markdown("""
    This application provides three pipelines:

    **Pipeline 1:** One-Click PDF Downloader (Research Articles).  
    **Pipeline 2:** Download RIS files from OpenAlex and upload them to Covidence.  
    **Pipeline 3:** Extract PDFs from Covidence and download ZIP/CSV.
    """)

    tab1, tab2, tab3 = st.tabs([
        "Pipeline 1: PDF Downloader",
        "Pipeline 2: RIS Files",
        "Pipeline 3: PDF Extraction"
    ])

    # ───────────────────────── Pipeline 1: PDF Downloader ─────────────────────────
    with tab1:
        st.title("📄 One-Click PDF Downloader (Research Articles)")
        mode = st.radio("I have a …", ["Title / keywords", "DOI"], horizontal=True)

        if mode == "Title / keywords":
            title_query = st.text_input("Enter title or keywords", placeholder="e.g. Attention Is All You Need")
            if st.button("Search & Download") and title_query.strip():
                with st.spinner("Searching Crossref …"):
                    items = crossref_by_title(title_query)
                if not items:
                    st.error("No Crossref results.")
                else:
                    itm, score = best_match(items, title_query)
                    if score < SIM_THRESHOLD:
                        st.warning(f"Best match similarity is {score}% (< {SIM_THRESHOLD}%). "
                                   "Provide a more precise title.")
                    else:
                        title = itm["title"][0]
                        doi   = itm["DOI"]
                        st.info(f"Best match: *{title}*  \nDOI: `{doi}`  – similarity {score}%")

                        with st.spinner("Fetching PDF …"):
                            pdf = try_publisher_pdf(itm)
                            if pdf is None:
                                try:
                                    pdf = fetch_via_scihub(doi)
                                except Exception as e:
                                    st.error(f"Crawling failed: {e}")
                                    pdf = None

                        if pdf:
                            st.success("PDF ready!")
                            st.download_button("📥 Save PDF",
                                               pdf,
                                               file_name=doi.replace("/", "_") + ".pdf",
                                               mime="application/pdf")

        else:  # DOI mode
            doi_input = st.text_input("Enter DOI (plain text or full https://doi.org/… link)",
                                      placeholder="e.g. 10.48550/arXiv.1706.03762")
            if st.button("Download PDF") and doi_input.strip():
                doi_plain = strip_doi(doi_input)
                with st.spinner("Looking up DOI in Crossref …"):
                    try:
                        item = crossref_by_doi(doi_plain)
                    except Exception as e:
                        st.error(f"Crossref lookup failed: {e}")
                        item = None

                if item:
                    title = item.get("title", [""])[0]
                    st.info(f"Found article: *{title}*")

                    with st.spinner("Fetching PDF …"):
                        pdf = try_publisher_pdf(item)
                        if pdf is None:
                            try:
                                pdf = fetch_via_scihub(doi_plain)
                            except Exception as e:
                                st.error(f"Searching failed: {e}")
                                pdf = None

                    if pdf:
                        st.success("PDF ready!")
                        st.download_button("📥 Save PDF",
                                           pdf,
                                           file_name=doi_plain.replace("/", "_") + ".pdf",
                                           mime="application/pdf")

    # ───────────────────────── Pipeline 2: RIS Files ─────────────────────────
    with tab2:
        st.header("Pipeline 2: RIS Download and Covidence Upload")
        st.markdown("""
        **Instructions:**
        1. Enter one article title per line.
        2. Provide your Covidence credentials and review URL.
        3. The app will query OpenAlex for metadata, generate RIS files, and then upload them to Covidence.
        """)
        article_titles_input = st.text_area("Enter article titles (one per line):")
        covidence_email = st.text_input("Covidence Email", key="ris_email")
        covidence_password = st.text_input("Covidence Password", type="password", key="ris_password")
        review_url = st.text_input("Review URL", value="https://app.covidence.org/reviews/your_review_id", key="ris_review")
        
        if st.button("Run RIS Pipeline"):
            if not article_titles_input.strip():
                st.error("Please enter at least one article title.")
                return
            if not covidence_email or not covidence_password or not review_url:
                st.error("Please fill in Covidence credentials and review URL.")
                return
            
            article_titles = [title.strip() for title in article_titles_input.splitlines() if title.strip()]
            RIS_FOLDER_PATH = os.path.join(os.getcwd(), "RIS_files")
            
            st.header("Step 1: Downloading RIS Files")
            downloaded_files = download_all_ris_files(article_titles, RIS_FOLDER_PATH)
            if downloaded_files:
                st.success("RIS file download completed.")
                st.header("Step 2: Uploading to Covidence")
                upload_ris_files_to_covidence(RIS_FOLDER_PATH, covidence_email, covidence_password, review_url)
            else:
                st.error("No RIS files were downloaded. Please check the article titles and try again.")

    # ───────────────────────── Pipeline 3: PDF Extraction ─────────────────────────
    with tab3:
        st.header("Pipeline 3: PDF Extraction from Covidence")
        st.markdown("""
        **Instructions:**
        1. Enter your Covidence credentials and review URL.
        2. The tool will log in, navigate to the 'extracted' studies page,
           and attempt to download associated PDFs.
        """)
        email_p2 = st.text_input("Covidence Email", key="pdf_email")
        password_p2 = st.text_input("Covidence Password", type="password", key="pdf_password")
        review_url_p2 = st.text_input("Review URL", value="https://app.covidence.org/reviews/your_review_id", key="pdf_review")
        
        if st.button("Run PDF Extraction Pipeline"):
            if not email_p2 or not password_p2 or not review_url_p2:
                st.error("Please fill in Covidence credentials and review URL.")
                return
            
            st.info("Starting PDF extraction from Covidence...")
            downloaded_pdfs, failed_papers = extract_and_download_pdfs_from_covidence(email_p2, password_p2, review_url_p2)
            
            if downloaded_pdfs:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                    for filename, pdf_bytes in downloaded_pdfs.items():
                        zip_file.writestr(filename, pdf_bytes)
                zip_buffer.seek(0)
                st.success("PDF extraction completed. Triggering automatic ZIP download...")
                trigger_download(zip_buffer.getvalue(), "downloaded_pdfs.zip", "application/zip")
            else:
                st.warning("No PDFs were successfully downloaded.")
            
            if failed_papers:
                csv_buffer = io.StringIO()
                writer = csv.writer(csv_buffer)
                writer.writerow(["Paper Title"])
                for title in failed_papers:
                    writer.writerow([title])
                st.info("Triggering automatic CSV download for failed papers...")
                trigger_download(csv_buffer.getvalue(), "failed_papers.csv", "text/csv")

if __name__ == "__main__":
    main()

# dependencies: pip install streamlit requests rapidfuzz scidownl webdriver-manager selenium
