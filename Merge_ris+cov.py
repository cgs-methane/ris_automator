import os
# Disable Streamlit file watcher to prevent inotify watch limit errors.
os.environ["STREAMLIT_SERVER_FILE_WATCHER"] = "none"

import time
import shutil
import requests
import streamlit as st
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

#############################
# Helper: Locate Chrome/Chromium Binary
#############################

def find_chrome_binary():
    # First, check for an environment variable (commonly used on some platforms)
    if "GOOGLE_CHROME_BIN" in os.environ:
        return os.environ["GOOGLE_CHROME_BIN"]
    # Try using shutil.which for common binary names.
    possible_binaries = ["google-chrome", "chromium-browser", "chromium"]
    for binary in possible_binaries:
        path = shutil.which(binary)
        if path:
            return path
    # Fallback: check common installation paths.
    common_paths = ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]
    for path in common_paths:
        if os.path.exists(path):
            return path
    return None

#############################
# Section 1: RIS Download Functions
#############################

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

def download_ris_for_article(article_title, output_folder):
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
    
    safe_title = "".join(c for c in article_title if c.isalnum() or c in (' ', '_', '-')).rstrip()
    filename = os.path.join(output_folder, f"{safe_title}.ris")
    
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
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    downloaded_files = []
    for title in article_titles:
        st.write(f"Processing article: {title}")
        ris_file = download_ris_for_article(title, output_folder)
        if ris_file:
            downloaded_files.append(ris_file)
    return downloaded_files

#############################
# Section 2: Covidence Upload Functions
#############################

def upload_ris_files_to_covidence(ris_folder_path, covidence_email, covidence_password, review_url):
    # Set up Chrome options with headless mode and cloud-friendly arguments.
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--remote-debugging-port=9222")
    
    chrome_binary = find_chrome_binary()
    if chrome_binary:
        st.write(f"Using Chrome binary: {chrome_binary}")
        chrome_options.binary_location = chrome_binary
    else:
        st.error("Chrome or Chromium browser not found on the system. Please ensure one is installed and/or set the GOOGLE_CHROME_BIN environment variable.")
        return

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )
    except Exception as e:
        st.error(f"Error initializing Chrome WebDriver: {e}")
        return
    
    try:
        # Login to Covidence
        driver.get('https://app.covidence.org/sign_in')
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
    
        ris_files = [os.path.join(ris_folder_path, f) for f in os.listdir(ris_folder_path) if f.lower().endswith('.ris')]
    
        for ris_file in ris_files:
            st.write(f"Uploading {os.path.basename(ris_file)}")
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
            except Exception as e:
                st.warning(f"Upload status not confirmed for {os.path.basename(ris_file)}. Exception: {e}")
    
        st.success("All RIS files have been processed for upload.")
    finally:
        driver.quit()

#############################
# Section 3: Streamlit Interface
#############################

st.title("RIS Download and Covidence Upload Pipeline")

st.markdown("""
This app downloads RIS files for a list of article titles from the OpenAlex API and uploads them to Covidence.
Please provide the required inputs below.
""")

# Input for article titles (one per line)
article_titles_input = st.text_area("Enter article titles (one per line):")
covidence_email = st.text_input("Covidence Email")
covidence_password = st.text_input("Covidence Password", type="password")
review_url = st.text_input("Review URL", value="https://app.covidence.org/reviews/your_review_id")

if st.button("Run Pipeline"):
    if not article_titles_input.strip():
        st.error("Please enter at least one article title.")
    elif not covidence_email or not covidence_password or not review_url:
        st.error("Please fill in all Covidence credentials and the review URL.")
    else:
        article_titles = [title.strip() for title in article_titles_input.splitlines() if title.strip()]
        RIS_FOLDER_PATH = os.path.join(os.getcwd(), "RIS_files")
        
        st.header("Step 1: Downloading RIS Files")
        downloaded_files = download_all_ris_files(article_titles, RIS_FOLDER_PATH)
        
        if downloaded_files:
            st.success("RIS file download completed.")
            
            st.header("Step 2: Uploading to Covidence")
            upload_ris_files_to_covidence(RIS_FOLDER_PATH, covidence_email, covidence_password, review_url)
            st.success("Covidence upload completed.")
        else:
            st.error("No RIS files were downloaded. Check the article titles and try again.")
