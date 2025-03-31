import os
import sys
import time
import shutil
import requests
import streamlit as st
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

# Use Firefox instead of Chrome
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager

#############################
# Utility: Ensure Firefox is available
#############################
def find_firefox_binary():
    """
    Check common places for Firefox or Firefox-ESR. 
    Return the path if found, otherwise return None.
    """
    possible_bins = ["firefox-esr", "firefox"]
    for bin_name in possible_bins:
        bin_path = shutil.which(bin_name)
        if bin_path:
            return bin_path
    return None

#############################
# Section 1: RIS Download Functions
#############################

def reconstruct_abstract(inverted_index):
    """
    Given an abstract_inverted_index from OpenAlex,
    reconstruct the abstract text.
    """
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
    """
    Format the retrieved metadata into a RIS entry (TY = JOUR).
    """
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
    """
    Search OpenAlex for the first result matching 'article_title',
    retrieve metadata, generate and save RIS file as '{file_index}.ris'.
    """
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
    
    # Take the first result
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
    """
    For each title in 'article_titles', attempt to download an RIS file.
    If the folder exists, remove it first, then create a fresh one.
    Each downloaded file will be named '1.ris', '2.ris', etc.
    """
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

#############################
# Section 2: Covidence Automation Functions (Using Firefox)
#############################

def upload_ris_files_to_covidence(ris_folder_path, covidence_email, covidence_password, review_url):
    """
    Launch Firefox in headless mode, log into Covidence, 
    and upload each RIS file.
    """
    firefox_path = find_firefox_binary()
    if not firefox_path:
        st.error(
            "Firefox binary not found on this system.\n"
            "Please ensure firefox-esr is added to packages.txt."
        )
        return

    firefox_options = FirefoxOptions()
    firefox_options.add_argument("--headless")
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--disable-gpu")
    firefox_options.binary_location = firefox_path
    
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
    
        ris_files = [
            os.path.join(ris_folder_path, f) 
            for f in os.listdir(ris_folder_path) 
            if f.lower().endswith('.ris')
        ]
        
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

def scrape_and_download_ris_from_covidence(review_url, covidence_email, covidence_password, output_folder):
    """
    Log in to Covidence, navigate to the given review,
    click on the 'extracted' studies link, click 'Load more' until all studies
    are visible, scrape the paper names, and download each RIS file from OpenAlex.
    """
    firefox_path = find_firefox_binary()
    if not firefox_path:
        st.error(
            "Firefox binary not found on this system.\n"
            "Please ensure firefox-esr is added to packages.txt."
        )
        return

    firefox_options = FirefoxOptions()
    firefox_options.add_argument("--headless")
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--disable-gpu")
    firefox_options.binary_location = firefox_path

    try:
        service = FirefoxService(executable_path=GeckoDriverManager().install())
        driver = webdriver.Firefox(service=service, options=firefox_options)
    except Exception as e:
        st.error(f"Error initializing Firefox WebDriver: {e}")
        return
    
    try:
        # Log into Covidence
        driver.get("https://app.covidence.org/sign_in")
        driver.maximize_window()
        time.sleep(3)
        driver.find_element(By.ID, 'session_email').send_keys(covidence_email)
        driver.find_element(By.NAME, 'session[password]').send_keys(covidence_password)
        driver.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]').click()
        time.sleep(5)
        
        # Navigate to the review page
        driver.get(review_url)
        time.sleep(3)
        
        # Click the "extracted" studies link (e.g., the link with '?filter=complete')
        try:
            extracted_link = driver.find_element(By.XPATH, "//a[contains(@href, '/review_studies/included?filter=complete')]")
            extracted_link.click()
            time.sleep(3)
        except Exception as e:
            st.error("Could not find the extracted studies link: " + str(e))
            return
        
        # Click "Load more" repeatedly until no button is found
        while True:
            try:
                load_more_button = driver.find_element(By.XPATH, "//button[contains(text(),'Load more')]")
                load_more_button.click()
                time.sleep(3)
            except Exception:
                break  # No more "Load more" button found

        # Scrape paper names from the loaded list.
        # (Note: adjust the selector based on the actual page structure.)
        paper_elements = driver.find_elements(By.XPATH, "//div[contains(@class, 'Extraction-StudyList') and contains(@class, 'item')]")
        paper_names = [elem.text.strip() for elem in paper_elements if elem.text.strip() != ""]
        # Remove duplicates
        paper_names = list(set(paper_names))
        st.write(f"Found {len(paper_names)} extracted papers:")
        for name in paper_names:
            st.write(name)
        
        # Create output folder for RIS files (remove if exists)
        if os.path.exists(output_folder):
            shutil.rmtree(output_folder)
        os.makedirs(output_folder)
        
        # Download RIS for each paper from OpenAlex
        downloaded_files = []
        for i, paper in enumerate(paper_names, start=1):
            st.write(f"Downloading RIS for paper: {paper}")
            ris_file = download_ris_for_article(paper, output_folder, i)
            if ris_file:
                downloaded_files.append(ris_file)
        
        if downloaded_files:
            st.success("Downloaded RIS files for extracted papers.")
        else:
            st.warning("No RIS files were downloaded for extracted papers.")
    finally:
        driver.quit()

#############################
# Section 3: Streamlit Interface
#############################

def main():
    st.title("RIS Download and Covidence Automation Pipeline")
    st.markdown("""
    This app downloads RIS files from the OpenAlex API and automates Covidence operations.
    
    **Instructions**:
    1. You can either provide a list of article titles (one per line) to download RIS files manually **or**
       choose to scrape the extracted papers from your Covidence review page.
    2. Provide your Covidence email, password, and review URL.
    3. For manual download, enter one article title per line and press 'Run Manual RIS Download'.
       For scraping, press 'Scrape & Download RIS from Covidence'.
    """)

    # Common Covidence credentials and review URL
    covidence_email = st.text_input("Covidence Email")
    covidence_password = st.text_input("Covidence Password", type="password")
    review_url = st.text_input("Review URL", value="https://app.covidence.org/reviews/your_review_id")
    
    # Option 1: Manual RIS file download (using OpenAlex search)
    st.header("Option 1: Manual RIS Download")
    article_titles_input = st.text_area("Enter article titles (one per line):")
    if st.button("Run Manual RIS Download"):
        if not article_titles_input.strip():
            st.error("Please enter at least one article title.")
        elif not covidence_email or not covidence_password or not review_url:
            st.error("Please fill in Covidence credentials and review URL.")
        else:
            article_titles = [title.strip() for title in article_titles_input.splitlines() if title.strip()]
            RIS_FOLDER_PATH = os.path.join(os.getcwd(), "RIS_files_manual")
            st.header("Step 1: Downloading RIS Files")
            downloaded_files = download_all_ris_files(article_titles, RIS_FOLDER_PATH)
            if downloaded_files:
                st.success("RIS file download completed.")
                st.header("Step 2: (Optional) Uploading to Covidence")
                upload_ris_files_to_covidence(RIS_FOLDER_PATH, covidence_email, covidence_password, review_url)
            else:
                st.error("No RIS files were downloaded. Check the article titles and try again.")
    
    st.markdown("---")
    
    # Option 2: Scrape extracted papers from Covidence and download RIS files using OpenAlex
    st.header("Option 2: Scrape & Download RIS from Covidence Extracted Papers")
    if st.button("Scrape & Download RIS from Covidence"):
        if not covidence_email or not covidence_password or not review_url:
            st.error("Please fill in Covidence credentials and review URL.")
        else:
            RIS_FOLDER_PATH = os.path.join(os.getcwd(), "RIS_files_extracted")
            scrape_and_download_ris_from_covidence(review_url, covidence_email, covidence_password, RIS_FOLDER_PATH)

if __name__ == "__main__":
    main()
