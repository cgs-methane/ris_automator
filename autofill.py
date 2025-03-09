import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def upload_ris_files_to_covidence(ris_folder_path, covidence_email, covidence_password, review_url):
    """
    Upload all .ris files in 'ris_folder_path' to Covidence using 
    Chrome (via webdriver_manager).
    """
    # Initialize the Chrome WebDriver
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    try:
        # 1. Navigate to Covidence login page
        driver.get('https://app.covidence.org/sign_in')
        driver.maximize_window()
        time.sleep(3)

        # 2. Log in to Covidence
        email_field = driver.find_element(By.ID, 'session_email')
        email_field.send_keys(covidence_email)

        password_field = driver.find_element(By.NAME, 'session[password]')
        password_field.send_keys(covidence_password)

        sign_in_button = driver.find_element(By.XPATH, '//form[@action="/session"]//input[@type="submit"]')
        sign_in_button.click()
        time.sleep(5)

        # 3. Navigate to the specific review page
        driver.get(review_url)  # e.g., 'https://app.covidence.org/reviews/549789'
        time.sleep(3)

        # 4. Get list of RIS files to upload
        ris_files = [
            os.path.join(ris_folder_path, f) 
            for f in os.listdir(ris_folder_path) 
            if f.lower().endswith('.ris')
        ]

        for ris_file in ris_files:
            print(f"Uploading {os.path.basename(ris_file)}")

            # 5. Navigate to the "Import" page before each upload
            driver.get(review_url + '/citation_imports/new')
            time.sleep(3)

            # 6. Fill out the import form
            select_import_into = Select(driver.find_element(By.NAME, 'citation_import[study_category]'))
            select_import_into.select_by_visible_text('Screen')

            upload_field = driver.find_element(By.ID, 'citation_import_file')
            upload_field.send_keys(ris_file)

            # 7. Submit the import
            import_button = driver.find_element(By.ID, 'upload-citations')
            import_button.click()
            time.sleep(5)

            # 8. Check for success message
            try:
                success_message = driver.find_element(By.CLASS_NAME, 'notifications').text
                print(f"Upload Status for {os.path.basename(ris_file)}:", success_message)
            except Exception as e:
                print(f"No success message found for {os.path.basename(ris_file)}. Check status manually.")
                print(f"Exception: {e}")

    finally:
        # 9. Quit the browser
        driver.quit()


if __name__ == "__main__":
    """
    Example usage:
    This just shows how you might call the function directly.
    For the pipeline, you'll import and call from another script.
    """
    # Example constants
    RIS_FOLDER_PATH = '/path/to/RIS_AMM'
    COVIDENCE_EMAIL = 'your_email@example.com'
    COVIDENCE_PASSWORD = 'your_password'
    REVIEW_URL = 'https://app.covidence.org/reviews/549789'
    
    upload_ris_files_to_covidence(
        ris_folder_path=RIS_FOLDER_PATH,
        covidence_email=COVIDENCE_EMAIL,
        covidence_password=COVIDENCE_PASSWORD,
        review_url=REVIEW_URL
    )
