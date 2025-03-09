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
    # Check environment for both firefox-esr and firefox
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

def download_ris_for_article(article_title, output_folder):
    """
    Search OpenAlex for the first result matching 'article_title',
    retrieve metadata, generate and save RIS file.
    """
    search_url = "https://api.openalex.org/works"
    params = {"search": article_title}
    
    # Query OpenAlex
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
    
    # Build RIS content
    ris_content = create_ris_entry(title, authors, year, doi, abstract)
    
    # Clean up filename
    safe_title = "".join(c for c in article_title if c.isalnum() or c in (' ', '_', '-')).rstrip()
    filename = os.path.join(output_folder, f"{safe_title}.ris")
    
    # Save .ris
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
    """
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
# Section 2: Covidence Upload Functions (Using Firefox)
#############################

def upload_ris_files_to_covidence(ris_folder_path, covidence_email, covidence_password, review_url):
    """
    Launch Firefox in headless mode, log into Covidence, 
    and upload each RIS file.
    """
    # Check for Firefox
    firefox_path = find_firefox_binary()
    if not firefox_path:
        st.error(
            "Firefox binary not found on this system.\n"
            "Please ensure firefox-esr is added to packages.txt."
        )
        return

    # Set up Firefox options for headless operation
    firefox_options = FirefoxOptions()
    firefox_options.add_argument("--headless")
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--disable-gpu")
    firefox_options.binary_location = firefox_path
    
    # Initialize the WebDriver
    try:
        service = FirefoxService(executable_path=GeckoDriverManager().install())
        driver = webdriver.Firefox(service=service, options=firefox_options)
    except Exception as e:
        st.error(f"Error initializing Firefox WebDriver: {e}")
        return
    
    try:
        # 1. Go to Covidence sign-in page
        driver.get("https://app.covidence.org/sign_in")
        driver.maximize_window()
        time.sleep(3)
        
        # 2. Fill out login form
        email_field = driver.find_element(By.ID, 'session_email')
        email_field.send_keys(covidence_email)
    
        password_field = driver.find_element(By.NAME, 'session[password]')
        password_field.send_keys(covidence_password)
    
        sign_in_button = driver.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]')
        sign_in_button.click()
        time.sleep(5)
    
        # 3. Open your review page
        driver.get(review_url)
        time.sleep(3)
    
        # 4. Get list of all local RIS files
        ris_files = [
            os.path.join(ris_folder_path, f) 
            for f in os.listdir(ris_folder_path) 
            if f.lower().endswith('.ris')
        ]
        
        # 5. For each RIS file, go to 'Import references' page, upload, submit
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
    
            # Attempt to read the success message
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

def main():
    st.title("RIS Download and Covidence Upload Pipeline")
    st.markdown("""
    This app downloads RIS files for a list of article titles from the OpenAlex API 
    and uploads them to Covidence.
    
    **Instructions**:
    1. Enter one article title per line in the text area.
    2. Provide Covidence email and password.
    3. Provide your Covidence review URL (e.g. `https://app.covidence.org/reviews/12345`).
    4. Press 'Run Pipeline'.
    """)

    # 1. Input for article titles (one per line)
    article_titles_input = st.text_area("Enter article titles (one per line):")
    
    # 2. Covidence credentials
    covidence_email = st.text_input("Covidence Email")
    covidence_password = st.text_input("Covidence Password", type="password")
    
    # 3. Covidence review URL (replace 'your_review_id' with your actual ID)
    review_url = st.text_input("Review URL", value="https://app.covidence.org/reviews/your_review_id")
    
    # Run the pipeline
    if st.button("Run Pipeline"):
        if not article_titles_input.strip():
            st.error("Please enter at least one article title.")
            return
        
        if not covidence_email or not covidence_password or not review_url:
            st.error("Please fill in Covidence credentials and review URL.")
            return
        
        article_titles = [
            title.strip() for title in article_titles_input.splitlines() 
            if title.strip()
        ]
        
        # Where to store .ris files
        RIS_FOLDER_PATH = os.path.join(os.getcwd(), "RIS_files")
        
        st.header("Step 1: Downloading RIS Files")
        downloaded_files = download_all_ris_files(article_titles, RIS_FOLDER_PATH)
        
        if downloaded_files:
            st.success("RIS file download completed.")
            
            st.header("Step 2: Uploading to Covidence")
            upload_ris_files_to_covidence(RIS_FOLDER_PATH, covidence_email, covidence_password, review_url)
        else:
            st.error("No RIS files were downloaded. Check the article titles and try again.")

if __name__ == "__main__":
    main()
